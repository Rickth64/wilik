import secrets
import re

from flask import Blueprint, jsonify, request

from models import AppSettings, Claim, Gift, User, db

public_bp = Blueprint("public", __name__, url_prefix="/api")


def normalize_claim_email(value):
    email = (value or "").strip().lower()
    if not email:
        return None
    if len(email) > 255 or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
        return None
    return email


def parse_claim_quantity(value):
    if type(value) is not int or value < 1:
        return None
    return value


@public_bp.route("/public/directory")
def public_directory():
    # lets visitors find a wishlist without needing its share link -- admin-toggleable
    # since it also means every list becomes discoverable to anyone who reaches this page
    settings = AppSettings.query.get(1)
    if not settings.public_directory_enabled:
        return jsonify({"enabled": False, "lists": []})
    users = User.query.filter_by(show_in_directory=True).order_by(User.list_name).all()
    lists = [
        {
            "list_name": user.list_name or f"{user.username}'s wishlist",
            "username": user.username,
            "share_token": user.share_token,
            "theme_color": user.theme_color,
        }
        for user in users
    ]
    return jsonify({"enabled": True, "lists": lists})


@public_bp.route("/public/<token>")
def public_wishlist(token):
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(user.public_dict())


@public_bp.route("/public/<token>/items")
def public_items(token):
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    gifts = Gift.query.filter_by(owner_id=user.id, received=False).all()
    return jsonify([gift.to_dict(include_claim_status=True) for gift in gifts])


@public_bp.route("/public/<token>/items/<int:item_id>/claim", methods=["POST"])
def claim_item(token, item_id):
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    gift = db.get_or_404(Gift, item_id)
    if gift.owner_id != user.id:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    claim_token = data.get("claim_token")
    if claim_token:
        claim = Claim.query.filter_by(gift_id=gift.id, claim_token=claim_token).first()
        if claim is None:
            return jsonify({"error": "Claim token not found"}), 403
        matching_claims = [claim]
    else:
        email = normalize_claim_email(data.get("email"))
        if email is None:
            return jsonify({"error": "Enter a valid email address"}), 400
        matching_claims = Claim.query.filter(
            Claim.gift_id == gift.id, db.func.lower(Claim.claimed_email) == email
        ).order_by(Claim.id).all()

    quantity = parse_claim_quantity(data.get("quantity", 1))
    if quantity is None:
        return jsonify({"error": "Enter a valid quantity"}), 400

    claimed_count = sum(claim.claimed_quantity for claim in gift.claims)
    if gift.quantity is not None and claimed_count + quantity > gift.quantity:
        return jsonify({"error": "Already claimed"}), 409

    if matching_claims:
        claim = matching_claims[0]
        claim.claimed_quantity = sum(item.claimed_quantity for item in matching_claims) + quantity
        for duplicate in matching_claims[1:]:
            db.session.delete(duplicate)
    else:
        claim = Claim(
            gift_id=gift.id,
            claimed_email=email,
            claim_token=secrets.token_urlsafe(24),
            claimed_quantity=quantity,
        )
        db.session.add(claim)
    db.session.commit()
    result = gift.to_dict(include_claim_status=True)
    result["claim_token"] = claim.claim_token  # only ever returned here, to the claimer themselves
    result["claim_quantity"] = claim.claimed_quantity
    return jsonify(result)


@public_bp.route("/public/<token>/items/<int:item_id>/verify-claim", methods=["POST"])
def verify_claim(token, item_id):
    """Confirms a claim token or email without releasing the claim."""
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    gift = db.get_or_404(Gift, item_id)
    if gift.owner_id != user.id:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    claim_token = data.get("claim_token")
    if claim_token:
        claim = Claim.query.filter_by(gift_id=gift.id, claim_token=claim_token).first()
        if claim is None:
            return jsonify({"error": "Claim token not found"}), 403
    else:
        email = normalize_claim_email(data.get("email"))
        if email is None:
            return jsonify({"error": "Enter a valid email address"}), 400
        claim = Claim.query.filter(
            Claim.gift_id == gift.id, db.func.lower(Claim.claimed_email) == email
        ).order_by(Claim.id).first()
    if claim is None:
        return jsonify({"error": "That email address doesn't match this claim"}), 403
    return jsonify({"claim_token": claim.claim_token, "claim_quantity": claim.claimed_quantity})


@public_bp.route("/public/<token>/items/<int:item_id>/unclaim", methods=["POST"])
def unclaim_item(token, item_id):
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    gift = db.get_or_404(Gift, item_id)
    if gift.owner_id != user.id:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    claim_token = data.get("claim_token")
    email = normalize_claim_email(data.get("email"))

    claim = None
    if claim_token:
        claim = Claim.query.filter_by(gift_id=gift.id, claim_token=claim_token).first()
        if claim is None and not email:
            return jsonify({"error": "Claim token not found"}), 403
    if claim is None and email:
        claim = Claim.query.filter(
            Claim.gift_id == gift.id, db.func.lower(Claim.claimed_email) == email
        ).first()
    if claim is None:
        return jsonify({"error": "That email address doesn't match this claim"}), 403

    quantity = parse_claim_quantity(data.get("quantity", claim.claimed_quantity))
    if quantity is None:
        return jsonify({"error": "Enter a valid quantity"}), 400
    if quantity > claim.claimed_quantity:
        return jsonify({"error": "You cannot release more than your claimed quantity"}), 400

    remaining_quantity = claim.claimed_quantity - quantity
    if remaining_quantity == 0:
        db.session.delete(claim)
    else:
        claim.claimed_quantity = remaining_quantity
    db.session.commit()
    result = gift.to_dict(include_claim_status=True)
    result["claim_quantity"] = remaining_quantity
    return jsonify(result)


@public_bp.route("/public/<token>/items/<int:item_id>/purchased", methods=["POST"])
def set_purchased(token, item_id):
    user = User.query.filter_by(share_token=token).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    gift = db.get_or_404(Gift, item_id)
    if gift.owner_id != user.id:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    claim = Claim.query.filter_by(gift_id=gift.id, claim_token=data.get("claim_token")).first()
    if claim is None:
        return jsonify({"error": "Claim it first"}), 400
    claim.purchased = bool(data.get("purchased", True))
    db.session.commit()
    return jsonify(gift.to_dict(include_claim_status=True))
