from email_validator import EmailNotValidError, validate_email
from flask import g
from flask import jsonify, request

from app import parallel_limiter
from app.alias_suffix import check_suffix_signature, verify_prefix_suffix
from app.alias_utils import check_alias_prefix
from app.api.base import api_bp, require_api_auth
from app.api.serializer import (
    serialize_alias_info_v2,
    get_alias_info_v2,
)
from app.config import MAX_NB_EMAIL_FREE_PLAN, ALIAS_LIMIT
from app.db import Session
from app.extensions import limiter
from app.log import LOG
from app.models import (
    Alias,
    AliasUsedOn,
    User,
    DeletedAlias,
    DomainDeletedAlias,
    Mailbox,
    AliasMailbox,
)
from app.utils import convert_to_id


@api_bp.route("/v2/alias/custom/new", methods=["POST"])
@require_api_auth
@limiter.limit(ALIAS_LIMIT)
@parallel_limiter.lock(name="alias_creation")
def new_custom_alias_v2():
    """
    Create a new custom alias
    Same as v1 but signed_suffix is actually the suffix with signature, e.g.
    .random_word@SL.co.Xq19rQ.s99uWQ7jD1s5JZDZqczYI5TbNNU
    Input:
        alias_prefix, for ex "www_groupon_com"
        signed_suffix, either .random_letters@simplelogin.co or @my-domain.com
        optional "hostname" in args
        optional "note"
    Output:
        201 if success
        409 if the alias already exists

    """
    user: User = g.user
    if not user.can_create_new_alias():
        LOG.d("user %s cannot create any custom alias", user)
        return (
            jsonify(
                error="You have reached the limitation of a free account with the maximum of "
                f"{MAX_NB_EMAIL_FREE_PLAN} aliases, please upgrade your plan to create more aliases"
            ),
            400,
        )

    hostname = request.args.get("hostname")

    data = request.get_json()
    if not data:
        LOG.i(f"User {user} tried to create an alias with empty data")
        return jsonify(error="request body cannot be empty"), 400

    alias_prefix = data.get("alias_prefix", "")
    if not isinstance(alias_prefix, str) or not alias_prefix:
        LOG.i(f"User {user} tried to create alias with invalid prefix")
        return jsonify(error="invalid value for alias_prefix"), 400

    alias_prefix = alias_prefix.strip().lower().replace(" ", "")
    signed_suffix = data.get("signed_suffix", "")
    if not isinstance(signed_suffix, str) or not signed_suffix:
        LOG.i(f"User {user} tried to create alias with invalid signed_suffix")
        return jsonify(error="invalid value for signed_suffix"), 400

    signed_suffix = signed_suffix.strip()

    note = data.get("note")
    alias_prefix = convert_to_id(alias_prefix)

    try:
        alias_suffix = check_suffix_signature(signed_suffix)
        if not alias_suffix:
            LOG.w("Alias creation time expired for %s", user)
            return jsonify(error="Alias creation time is expired, please retry"), 412
    except Exception:
        LOG.w("Alias suffix is tampered, user %s", user)
        return jsonify(error="Tampered suffix"), 400

    if not verify_prefix_suffix(user, alias_prefix, alias_suffix):
        LOG.i(f"User {user} tried to use invalid prefix or suffix")
        return jsonify(error="wrong alias prefix or suffix"), 400

    full_alias = alias_prefix + alias_suffix
    if (
        Alias.get_by(email=full_alias)
        or DeletedAlias.get_by(email=full_alias)
        or DomainDeletedAlias.get_by(email=full_alias)
    ):
        LOG.d(f"full alias already used {full_alias} for user {user}")
        return jsonify(error=f"alias {full_alias} already exists"), 409

    if ".." in full_alias:
        LOG.d(f"User {user} tried to create an alias with ..")
        return (
            jsonify(error="2 consecutive dot signs aren't allowed in an email address"),
            400,
        )

    try:
        alias = Alias.create(
            user_id=user.id,
            email=full_alias,
            mailbox_id=user.default_mailbox_id,
            note=note,
        )
    except EmailNotValidError:
        LOG.d(f"User {user} tried to create an alias with invalid email {full_alias}")
        return jsonify(error="Email is not valid"), 400

    Session.commit()
    LOG.i(f"User {user} created custom alias {alias} via API (v2)")

    if hostname:
        AliasUsedOn.create(alias_id=alias.id, hostname=hostname, user_id=alias.user_id)
        Session.commit()

    return (
        jsonify(alias=full_alias, **serialize_alias_info_v2(get_alias_info_v2(alias))),
        201,
    )


@api_bp.route("/v3/alias/custom/new", methods=["POST"])
@require_api_auth
@limiter.limit(ALIAS_LIMIT)
@parallel_limiter.lock(name="alias_creation")
def new_custom_alias_v3():
    """
    Create a new custom alias
    Same as v2 but accept a list of mailboxes as input
    Input:
        alias_prefix, for ex "www_groupon_com"
        signed_suffix, either .random_letters@simplelogin.co or @my-domain.com
        mailbox_ids: list of int
        optional "hostname" in args
        optional "note"
        optional "name"

    Output:
        201 if success
        409 if the alias already exists

    """
    user: User = g.user
    if not user.can_create_new_alias():
        LOG.d("user %s cannot create any custom alias", user)
        return (
            jsonify(
                error="You have reached the limitation of a free account with the maximum of "
                f"{MAX_NB_EMAIL_FREE_PLAN} aliases, please upgrade your plan to create more aliases"
            ),
            400,
        )

    hostname = request.args.get("hostname")

    data = request.get_json()
    if not data:
        LOG.i(f"User {user} tried to create an alias with empty data")
        return jsonify(error="request body cannot be empty"), 400

    if not isinstance(data, dict):
        LOG.i(f"User {user} tried to create an alias with invalid format")
        return jsonify(error="request body does not follow the required format"), 400

    alias_prefix_data = data.get("alias_prefix", "") or ""

    if not isinstance(alias_prefix_data, str):
        LOG.i(f"User {user} tried to create an alias with data as string")
        return jsonify(error="request body does not follow the required format"), 400

    alias_prefix = alias_prefix_data.strip().lower().replace(" ", "")
    signed_suffix = data.get("signed_suffix", "") or ""

    if not isinstance(signed_suffix, str):
        LOG.i(f"User {user} tried to create an alias with invalid signed_suffix")
        return jsonify(error="request body does not follow the required format"), 400

    signed_suffix = signed_suffix.strip()

    mailbox_ids = data.get("mailbox_ids")
    note = data.get("note")
    name = data.get("name")
    if name:
        name = name.replace("\n", "")
    alias_prefix = convert_to_id(alias_prefix)

    if not check_alias_prefix(alias_prefix):
        LOG.i(f"User {user} tried to create an alias with invalid prefix or too long")
        return jsonify(error="alias prefix invalid format or too long"), 400

    # check if mailbox is not tempered with
    if not isinstance(mailbox_ids, list):
        LOG.i(f"User {user} tried to create an alias with invalid mailbox array")
        return jsonify(error="mailbox_ids must be an array of id"), 400
    mailboxes = []
    for mailbox_id in mailbox_ids:
        mailbox = Mailbox.get(mailbox_id)
        if not mailbox or mailbox.user_id != user.id or not mailbox.verified:
            LOG.i(f"User {user} tried to create an alias with invalid mailbox")
            return jsonify(error="Errors with Mailbox"), 400
        mailboxes.append(mailbox)

    if not mailboxes:
        LOG.i(f"User {user} tried to create an alias with missing mailbox")
        return jsonify(error="At least one mailbox must be selected"), 400

    # hypothesis: user will click on the button in the 600 secs
    try:
        alias_suffix = check_suffix_signature(signed_suffix)
        if not alias_suffix:
            LOG.i(f"User {user} tried to create an alias with expired suffix")
            LOG.w("Alias creation time expired for %s", user)
            return jsonify(error="Alias creation time is expired, please retry"), 412
    except Exception:
        LOG.i(f"User {user} tried to create an alias with tampered suffix")
        LOG.w("Alias suffix is tampered, user %s", user)
        return jsonify(error="Tampered suffix"), 400

    if not verify_prefix_suffix(user, alias_prefix, alias_suffix):
        LOG.i(f"User {user} tried to create an alias with invalid prefix or suffix")
        return jsonify(error="wrong alias prefix or suffix"), 400

    full_alias = alias_prefix + alias_suffix
    if (
        Alias.get_by(email=full_alias)
        or DeletedAlias.get_by(email=full_alias)
        or DomainDeletedAlias.get_by(email=full_alias)
    ):
        LOG.i(f"User {user} tried to create an alias with already used alias")
        return jsonify(error=f"alias {full_alias} already exists"), 409

    if ".." in full_alias:
        LOG.i(f"User {user} tried to create an alias with ..")
        return (
            jsonify(error="2 consecutive dot signs aren't allowed in an email address"),
            400,
        )
    try:
        validate_email(full_alias, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError as e:
        LOG.i(f"Could not validate email {full_alias} for custom alias creation: {e}")
        return (
            jsonify(error="Email alias is invalid"),
            400,
        )

    alias = Alias.create(
        user_id=user.id,
        email=full_alias,
        note=note,
        name=name or None,
        mailbox_id=mailboxes[0].id,
    )
    Session.flush()

    for i in range(1, len(mailboxes)):
        AliasMailbox.create(
            alias_id=alias.id,
            mailbox_id=mailboxes[i].id,
        )

    Session.commit()

    LOG.i(f"User {user} created custom alias {alias} via API (v3)")

    if hostname:
        AliasUsedOn.create(alias_id=alias.id, hostname=hostname, user_id=alias.user_id)
        Session.commit()

    return (
        jsonify(alias=full_alias, **serialize_alias_info_v2(get_alias_info_v2(alias))),
        201,
    )
