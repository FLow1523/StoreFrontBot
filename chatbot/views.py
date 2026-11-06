# Fixes for chatbot/views.py
# Apply the three changes below. Everything else in the file stays as it is.


# ---------------------------------------------------------------------------
# CHANGE 1: import. Replace this existing line near the top:
#     from django.db import IntegrityError, close_old_connections
# with:
# ---------------------------------------------------------------------------
from django.db import IntegrityError, close_old_connections, transaction


# ---------------------------------------------------------------------------
# CHANGE 2: replace the whole register_message() function.
#
# Problem: any IntegrityError was reported as "duplicate", so an unrelated
# database constraint failure (for example a unique constraint on sender_hash)
# made real messages look like duplicates and silently dropped them.
# Now it only counts as a duplicate if that message_id really exists.
# ---------------------------------------------------------------------------
def register_message(message_id, sender_hash):
    """
    Decide whether a message should be processed.
    Returns "new", "duplicate" or "rate_limited".
    """
    window_start = timezone.now() - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    recent = ProcessedMessage.objects.filter(
        sender_hash=sender_hash, created_at__gte=window_start
    ).count()
    if recent >= RATE_LIMIT_MESSAGES:
        return "rate_limited"

    try:
        with transaction.atomic():
            ProcessedMessage.objects.create(
                message_id=message_id, sender_hash=sender_hash
            )
    except IntegrityError:
        if ProcessedMessage.objects.filter(message_id=message_id).exists():
            return "duplicate"
        # The insert failed for some other reason. Do not drop the customer's
        # message: log the real error and process it anyway.
        logger.exception(
            "ProcessedMessage insert failed, but message id %s is not a duplicate",
            message_id[-8:],
        )
        return "new"
    return "new"


# ---------------------------------------------------------------------------
# CHANGE 3: in webhook(), replace the inner loop body that starts with
#     value = change.get("value", {})
#     for msg in value.get("messages", []):
# with the version below. It adds three log lines:
#   - one per webhook event, so you can see every POST that reaches the code
#   - one for failed delivery statuses reported by WhatsApp (with the error)
#   - the message id suffix on the duplicate log
# Only the lines marked NEW differ from your current code.
# ---------------------------------------------------------------------------
def _webhook_inner_loop_example(change):
    value = change.get("value", {})

    # NEW: log what each event contains
    logger.info(
        "Webhook event: messages=%d statuses=%d",
        len(value.get("messages", [])),
        len(value.get("statuses", [])),
    )

    # NEW: surface delivery failures that Meta reports after a send
    for status in value.get("statuses", []):
        if status.get("status") == "failed":
            logger.warning("WhatsApp reported a failed delivery: %s", status.get("errors"))

    for msg in value.get("messages", []):
        if msg.get("type") != "text":
            continue

        message_id = msg.get("id")
        from_number = msg.get("from")
        body = (msg.get("text") or {}).get("body")
        if not (message_id and from_number and isinstance(body, str)):
            continue

        text = body.strip()[:MAX_INCOMING_TEXT_LENGTH]
        if not text:
            continue

        outcome = register_message(message_id, hash_sender(from_number))
        if outcome == "duplicate":
            # NEW: include the id suffix so you can tell which message it was
            logger.info("Ignored a duplicate message delivery (id ...%s)", message_id[-8:])
            continue
        if outcome == "rate_limited":
            logger.warning(
                "Rate limit reached for %s; message ignored",
                mask_phone(from_number),
            )
            continue

        logger.info(
            "Accepted message from %s (%d characters)",
            mask_phone(from_number),
            len(text),
        )
        executor.submit(process_message, from_number, text)
