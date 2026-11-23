import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, jsonify
from flask_login import LoginManager
from flask_migrate import Migrate, downgrade, upgrade
from sqlalchemy import inspect, text

from models import AppSettings, Claim, Gift, User, db
from routes.items import items_bp
from routes.public import public_bp
from routes.public import public_bp


class ClaimManagementTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            TESTING=True,
        )
        db.init_app(self.app)
        login_manager = LoginManager(self.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        @login_manager.unauthorized_handler
        def unauthorized():
            return jsonify({"error": "Login required"}), 401

        self.app.register_blueprint(items_bp)
        self.app.register_blueprint(public_bp)

        with self.app.app_context():
            db.create_all()
            db.session.add(AppSettings(id=1, app_name="Wilik", claim_management_site_enabled=True))
            owner = User(username="owner", claim_management_enabled=False)
            other = User(username="other", claim_management_enabled=True)
            db.session.add_all([owner, other])
            db.session.flush()
            gift = Gift(owner_id=owner.id, title="Claimed gift", quantity=1)
            db.session.add(gift)
            db.session.flush()
            db.session.add(Claim(gift_id=gift.id, claimed_email="alice@example.com", claim_token="secret-token"))
            db.session.commit()
            self.owner_id = owner.id
            self.other_id = other.id
            self.gift_id = gift.id
            self.owner_share_token = owner.share_token

        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def login_as(self, user_id):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def set_claim_management(self, enabled):
        with self.app.app_context():
            owner = db.session.get(User, self.owner_id)
            owner.claim_management_enabled = enabled
            db.session.commit()

    def set_site_claim_management(self, enabled):
        with self.app.app_context():
            settings = db.session.get(AppSettings, 1)
            settings.claim_management_site_enabled = enabled
            db.session.commit()

    def test_disabled_wishlist_hides_claimed_count_but_allows_one_off_reveal(self):
        self.login_as(self.owner_id)

        items_response = self.client.get("/api/items")
        self.assertEqual(items_response.status_code, 200)
        self.assertNotIn("claimed_count", items_response.get_json()[0])

        summary_response = self.client.get(f"/api/items/{self.gift_id}/claim-info")
        self.assertEqual(summary_response.get_json(), {"claimed_count": 1})

        # opting out of the always-on toggle only turns off the passive lock icon --
        # the owner can still deliberately reveal a name once, e.g. right before deleting
        reveal_response = self.client.get(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reveal_response.status_code, 200)
        self.assertEqual(
            reveal_response.get_json(),
            {"claims": [{"email": "alice@example.com", "quantity": 1}]},
        )

        # resetting claims still requires the full per-wishlist opt-in
        reset_response = self.client.delete(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reset_response.status_code, 403)

    def test_enabled_owner_can_reveal_and_reset_claims(self):
        self.set_claim_management(True)
        self.login_as(self.owner_id)

        items_response = self.client.get("/api/items")
        self.assertEqual(items_response.get_json()[0]["claimed_count"], 1)

        reveal_response = self.client.get(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reveal_response.status_code, 200)
        self.assertEqual(
            reveal_response.get_json(),
            {"claims": [{"email": "alice@example.com", "quantity": 1}]},
        )
        self.assertNotIn("secret-token", reveal_response.get_data(as_text=True))

        reset_response = self.client.delete(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.get_json()["claimed_count"], 0)

        with self.app.app_context():
            self.assertEqual(Claim.query.filter_by(gift_id=self.gift_id).count(), 0)

    def test_non_owner_cannot_reveal_or_reset_claims(self):
        self.login_as(self.other_id)

        reveal_response = self.client.get(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reveal_response.status_code, 403)

        reset_response = self.client.delete(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reset_response.status_code, 403)

    def test_site_wide_disable_still_allows_one_off_reveal_but_blocks_reset(self):
        self.set_claim_management(True)
        self.set_site_claim_management(False)
        self.login_as(self.owner_id)

        items_response = self.client.get("/api/items")
        self.assertEqual(items_response.status_code, 200)
        self.assertNotIn("claimed_count", items_response.get_json()[0])

        # a one-off reveal (e.g. right before deleting) is never blocked, even with
        # claim management fully off site-wide -- only the passive surface is gated
        reveal_response = self.client.get(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reveal_response.status_code, 200)
        self.assertEqual(
            reveal_response.get_json(),
            {"claims": [{"email": "alice@example.com", "quantity": 1}]},
        )

        reset_response = self.client.delete(f"/api/items/{self.gift_id}/claims")
        self.assertEqual(reset_response.status_code, 403)

    def test_public_claim_requires_and_normalizes_email(self):
        with self.app.app_context():
            owner = db.session.get(User, self.owner_id)
            token = owner.share_token
            gift = db.session.get(Gift, self.gift_id)
            gift.quantity = None
            for claim in list(gift.claims):
                db.session.delete(claim)
            db.session.commit()

        invalid_response = self.client.post(
            f"/api/public/{token}/items/{self.gift_id}/claim",
            json={"email": "not-an-email"},
        )
        self.assertEqual(invalid_response.status_code, 400)

        claim_response = self.client.post(
            f"/api/public/{token}/items/{self.gift_id}/claim",
            json={"email": "  Alice@Example.COM  "},
        )
        self.assertEqual(claim_response.status_code, 200)

        with self.app.app_context():
            claim = Claim.query.filter_by(gift_id=self.gift_id).order_by(Claim.id.desc()).first()
            self.assertEqual(claim.claimed_email, "alice@example.com")

    def test_public_claim_rejects_invalid_email_addresses(self):
        invalid_emails = [
            "",
            "not-an-email",
            "missing-domain@example",
            "two words@example.com",
            f"{'a' * 256}@example.com",
        ]

        for email in invalid_emails:
            with self.subTest(email=email):
                response = self.client.post(
                    f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
                    json={"email": email},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": "Enter a valid email address"})

        with self.app.app_context():
            self.assertEqual(Claim.query.filter_by(gift_id=self.gift_id).count(), 1)

    def test_public_claim_email_is_normalized_and_matched_case_insensitively(self):
        with self.app.app_context():
            gift = db.session.get(Gift, self.gift_id)
            gift.quantity = None
            for claim in list(gift.claims):
                db.session.delete(claim)
            db.session.commit()

        claim_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "  Mixed.Case@Example.COM  "},
        )
        self.assertEqual(claim_response.status_code, 200)

        with self.app.app_context():
            new_claim = Claim.query.filter_by(claimed_email="mixed.case@example.com").one()

        verify_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/verify-claim",
            json={"email": "MIXED.CASE@EXAMPLE.COM"},
        )
        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(
            verify_response.get_json(),
            {"claim_token": new_claim.claim_token, "claim_quantity": 1},
        )

    def test_public_claim_and_unclaim_use_requested_quantities(self):
        with self.app.app_context():
            gift = db.session.get(Gift, self.gift_id)
            gift.quantity = 5
            for claim in list(gift.claims):
                db.session.delete(claim)
            db.session.commit()

        claim_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "alice@example.com", "quantity": 3},
        )
        self.assertEqual(claim_response.status_code, 200)
        claim_data = claim_response.get_json()
        self.assertEqual(claim_data["claimed_count"], 3)
        self.assertEqual(claim_data["claim_quantity"], 3)

        release_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/unclaim",
            json={"claim_token": claim_data["claim_token"], "quantity": 2},
        )
        self.assertEqual(release_response.status_code, 200)
        release_data = release_response.get_json()
        self.assertEqual(release_data["claimed_count"], 1)
        self.assertEqual(release_data["claim_quantity"], 1)

        too_many_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/unclaim",
            json={"claim_token": claim_data["claim_token"], "quantity": 2},
        )
        self.assertEqual(too_many_response.status_code, 400)

        final_release_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/unclaim",
            json={"claim_token": claim_data["claim_token"], "quantity": 1},
        )
        self.assertEqual(final_release_response.status_code, 200)
        self.assertEqual(final_release_response.get_json()["claimed_count"], 0)

    def test_public_claim_rejects_invalid_quantity_and_prevents_overclaiming(self):
        with self.app.app_context():
            gift = db.session.get(Gift, self.gift_id)
            gift.quantity = 5
            db.session.get(Claim, 1).claimed_quantity = 4
            db.session.commit()

        invalid_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "alice@example.com", "quantity": 0},
        )
        self.assertEqual(invalid_response.status_code, 400)

        overclaim_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "bob@example.com", "quantity": 2},
        )
        self.assertEqual(overclaim_response.status_code, 409)

        with self.app.app_context():
            self.assertEqual(db.session.get(Gift, self.gift_id).to_dict(True)["claimed_count"], 4)

    def test_claims_for_same_email_accumulate_and_verify_together(self):
        with self.app.app_context():
            gift = db.session.get(Gift, self.gift_id)
            gift.quantity = None
            db.session.commit()

        first_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "Alice@example.com", "quantity": 2},
        )
        self.assertEqual(first_response.status_code, 200)
        first_claim = first_response.get_json()

        second_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"email": "alice@EXAMPLE.com", "quantity": 2},
        )
        self.assertEqual(second_response.status_code, 200)
        second_claim = second_response.get_json()
        self.assertEqual(second_claim["claimed_count"], 5)
        self.assertEqual(second_claim["claim_quantity"], 5)
        self.assertEqual(second_claim["claim_token"], first_claim["claim_token"])

        token_claim_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/claim",
            json={"claim_token": second_claim["claim_token"], "quantity": 1},
        )
        self.assertEqual(token_claim_response.status_code, 200)
        token_claim = token_claim_response.get_json()
        self.assertEqual(token_claim["claimed_count"], 6)
        self.assertEqual(token_claim["claim_quantity"], 6)

        verify_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/verify-claim",
            json={"email": "ALICE@example.com"},
        )
        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(verify_response.get_json()["claim_quantity"], 6)

        token_verify_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/verify-claim",
            json={"claim_token": token_claim["claim_token"]},
        )
        self.assertEqual(token_verify_response.status_code, 200)
        self.assertEqual(token_verify_response.get_json()["claim_quantity"], 6)

        stale_token_response = self.client.post(
            f"/api/public/{self.owner_share_token}/items/{self.gift_id}/verify-claim",
            json={"claim_token": "stale-token"},
        )
        self.assertEqual(stale_token_response.status_code, 403)
        self.assertEqual(stale_token_response.get_json(), {"error": "Claim token not found"})

        with self.app.app_context():
            claims = Claim.query.filter_by(gift_id=self.gift_id).all()
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0].claimed_quantity, 6)


class ClaimManagementMigrationTestCase(unittest.TestCase):
    def test_upgrade_adds_default_off_column_and_downgrade_removes_it(self):
        migrations_directory = str(Path(__file__).parent / "migrations")

        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "migration-test.db"
            app = Flask(__name__)
            app.config.update(
                SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
                TESTING=True,
            )
            db.init_app(app)
            Migrate(app, db, directory=migrations_directory)

            with app.app_context():
                upgrade(directory=migrations_directory, revision="d4e5f6a7b8c9")
                columns_before = {column["name"] for column in inspect(db.engine).get_columns("user")}
                self.assertNotIn("claim_management_enabled", columns_before)

                upgrade(directory=migrations_directory, revision="head")
                columns_after = {
                    column["name"]: column for column in inspect(db.engine).get_columns("user")
                }
                self.assertIn("claim_management_enabled", columns_after)
                self.assertFalse(columns_after["claim_management_enabled"]["nullable"])

                downgrade(directory=migrations_directory, revision="d4e5f6a7b8c9")
                columns_downgraded = {column["name"] for column in inspect(db.engine).get_columns("user")}
                self.assertNotIn("claim_management_enabled", columns_downgraded)
                db.session.remove()
                db.engine.dispose()


class ClaimManagementSiteToggleMigrationTestCase(unittest.TestCase):
    def test_upgrade_adds_default_on_column_and_downgrade_removes_it(self):
        migrations_directory = str(Path(__file__).parent / "migrations")

        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "migration-test.db"
            app = Flask(__name__)
            app.config.update(
                SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
                TESTING=True,
            )
            db.init_app(app)
            Migrate(app, db, directory=migrations_directory)

            with app.app_context():
                upgrade(directory=migrations_directory, revision="e5f6a7b8c9d0")
                columns_before = {column["name"] for column in inspect(db.engine).get_columns("app_settings")}
                self.assertNotIn("claim_management_site_enabled", columns_before)

                upgrade(directory=migrations_directory, revision="head")
                columns_after = {
                    column["name"]: column for column in inspect(db.engine).get_columns("app_settings")
                }
                self.assertIn("claim_management_site_enabled", columns_after)
                self.assertFalse(columns_after["claim_management_site_enabled"]["nullable"])

                downgrade(directory=migrations_directory, revision="e5f6a7b8c9d0")
                columns_downgraded = {column["name"] for column in inspect(db.engine).get_columns("app_settings")}
                self.assertNotIn("claim_management_site_enabled", columns_downgraded)
                db.session.remove()
                db.engine.dispose()


class ClaimAggregationMigrationTestCase(unittest.TestCase):
    def test_upgrade_adds_quantity_merges_duplicate_claims_and_can_be_downgraded(self):
        migrations_directory = str(Path(__file__).parent / "migrations")

        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "migration-test.db"
            app = Flask(__name__)
            app.config.update(
                SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
                TESTING=True,
            )
            db.init_app(app)
            Migrate(app, db, directory=migrations_directory)

            with app.app_context():
                upgrade(directory=migrations_directory, revision="g7b8c9d0e1f2")
                owner = User(username="merge-owner")
                db.session.add(owner)
                db.session.flush()
                gift = Gift(owner_id=owner.id, title="Merge gift", quantity=10)
                db.session.add(gift)
                db.session.flush()
                for token, email in [
                    ("first-token", "Alice@Example.com"),
                    ("second-token", "alice@example.com"),
                ]:
                    db.session.execute(
                        text(
                            "INSERT INTO claim (gift_id, claimed_email, claim_token, purchased) "
                            "VALUES (:gift_id, :email, :token, 0)"
                        ),
                        {"gift_id": gift.id, "email": email, "token": token},
                    )
                db.session.commit()

                upgrade(directory=migrations_directory, revision="head")
                columns = {column["name"] for column in inspect(db.engine).get_columns("claim")}
                self.assertIn("claimed_quantity", columns)
                claims = db.session.execute(
                    text(
                        "SELECT claim_token, claimed_email, claimed_quantity "
                        "FROM claim WHERE gift_id = :gift_id"
                    ),
                    {"gift_id": gift.id},
                ).all()
                self.assertEqual(claims, [("first-token", "alice@example.com", 2)])
                unique_indexes = [
                    index
                    for index in inspect(db.engine).get_indexes("claim")
                    if index["name"] == "uq_claim_gift_claimed_email" and index["unique"]
                ]
                self.assertEqual(len(unique_indexes), 1)

                downgrade(directory=migrations_directory, revision="g7b8c9d0e1f2")
                downgraded_columns = {
                    column["name"] for column in inspect(db.engine).get_columns("claim")
                }
                self.assertNotIn("claimed_quantity", downgraded_columns)
                self.assertNotIn(
                    "uq_claim_gift_claimed_email",
                    {index["name"] for index in inspect(db.engine).get_indexes("claim")},
                )
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
