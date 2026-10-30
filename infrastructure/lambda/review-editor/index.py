# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
"""The hosted BuilderCards card editor: the maintainer's review UI, run as a Lambda.

The dedicated admin HTTP API (BuilderCardsAdminApi, payload format 2.0) invokes it; the public
upload API is a different Lambda. Every route lives under BASE_PATH (default "/review"):

- GET /review lists the cards (pending, approved or all); with ?status=rejected it shows the
  Rejected tab instead.
- GET /review/edit shows the edit form of one card.
- POST /review/save saves the edits and, while the card is approved, publishes it.
- GET and POST /review/delete confirm, then delete a card everywhere.
- POST /review/reprocess sends a rejected upload back into the processing path.
- GET and POST /review/reject-delete confirm, then delete a rejected upload.

Every route is gated by HTTP Basic Auth, which fails closed, and every POST needs a valid CSRF
token. Publishing and deleting a card are done by the internal committer Lambda, which this editor
invokes synchronously with nothing but an action and a cardId. The editor holds no GitHub token and
no power to delete cards, so a compromise of this internet-facing function cannot reach the
repository or purge data. The Rejected tab works on S3 directly: a rejected upload has no DynamoDB
item, so the picture in S3 is all there is.

Environment variables, set by Terraform:
    DDB_TABLE: The card table (default "BuilderCards").
    BASE_PATH: The URL prefix that every route, link and form action is built from, so the pages
        work behind it (default "/review").
    BASIC_AUTH_SECRET_ARN: The Secrets Manager secret holding JSON
        {"username": ..., "password": ...}. Both values are read at runtime, so neither is in the
        environment or the Terraform state.
    IMAGES_BUCKET: The private images bucket, for pre-signed previews and the rejected uploads.
    COMMITTER_FUNCTION_NAME: The committer Lambda, invoked to publish or delete a card.
    REJECTED_PREFIX: The quarantine prefix of rejected uploads (default "images/rejected/").
    RAW_PREFIX: The upload prefix that triggers the card-detect gate (default "images/raw/").
    FORCE_PROCESS_META: The S3 metadata key that "Put to process" sets, so the card-detect gate
        forwards the upload without scoring it again (default "force-process"). It must equal the
        gate's own setting, so Terraform sets both from one value.
    AWS_REGION: Set by the Lambda runtime.

It writes edited fields and publish state to existing DynamoDB items (never new ones), and moves or
deletes objects under the rejected prefix. It returns HTML pages, redirects and auth challenges.
"""

import os
import re
import time
import hmac
import html
import json
import base64
import hashlib
import urllib.parse

import boto3

TABLE = os.environ.get("DDB_TABLE", "BuilderCards")
REGION = os.environ.get("AWS_REGION", "eu-central-1")
BASE = os.environ.get("BASE_PATH", "/review")

BASIC_AUTH_SECRET_ARN = os.environ.get("BASIC_AUTH_SECRET_ARN", "")

IMAGES_BUCKET = os.environ.get("IMAGES_BUCKET", "")

COMMITTER_FUNCTION_NAME = os.environ.get("COMMITTER_FUNCTION_NAME", "")

REJECTED_PREFIX = os.environ.get("REJECTED_PREFIX", "images/rejected/")
RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
FORCE_PROCESS_META = os.environ.get("FORCE_PROCESS_META", "force-process")
REJECTED_URL_TTL = 3600

ddb = boto3.client("dynamodb", region_name=REGION)
secretsmanager = boto3.client("secretsmanager", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

CREDS_TTL_SECONDS = 300
_credentials = None
_credentials_fetched_at = 0.0


EDITABLE = ["title", "slug", "weight", "event", "year", "effect", "description", "category", "subcategory", "uploader", "status"]

DRAFTS = {
    "title": "title_ai_draft",
    "slug": "slug_ai_draft",
    "effect": "effect_ai_draft",
    "description": "description_ai_draft",
}

MULTILINE = {"effect", "description"}


def _s(item, key):
    """
    Reads a string attribute out of a raw DynamoDB item.

    The low-level client wraps every attribute in a type descriptor, so a string lives at
    item[key]["S"]. This unwraps it and returns an empty string when the item is None or the
    attribute is absent, so callers never have to guard each field individually.

    Args:
        item: Raw DynamoDB item dict, or None.
        key: Attribute name to read.

    Returns:
        The attribute's string value, or an empty string if missing.
    """
    return item.get(key, {}).get("S", "") if item else ""


def list_cards(status_filter=None):
    """
    Fetches every card item, optionally narrowed to a single status.

    When a status is given this queries the status-index GSI, which is cheap and reads only the
    matching partition; without one it falls back to a full table scan, which is acceptable at this
    catalog's size. Both paths follow LastEvaluatedKey to the end, so a result set larger than
    DynamoDB's 1 MB page limit is returned complete rather than silently truncated. Results are
    sorted by status and then creation time, which is the order the list view renders.

    Args:
        status_filter: Optional status to filter by, e.g. "pending" or "approved".

    Returns:
        List of raw DynamoDB items, sorted by (status, createdAt).
    """
    items = []
    if status_filter:
        kwargs = {
            "TableName": TABLE,
            "IndexName": "status-index",
            "KeyConditionExpression": "#s = :s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":s": {"S": status_filter}},
        }
        while True:
            resp = ddb.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    else:
        kwargs = {"TableName": TABLE}
        while True:
            resp = ddb.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    items.sort(key=lambda it: (_s(it, "status"), _s(it, "createdAt")))
    return items


def get_card(card_id):
    """
    Fetches a single card item by its identifier.

    Args:
        card_id: The card's primary key.

    Returns:
        The raw DynamoDB item dict, or None if no card with that id exists.
    """
    resp = ddb.get_item(TableName=TABLE, Key={"cardId": {"S": card_id}})
    return resp.get("Item")


def _finished_image_url(item, expires=3600):
    """
    Builds a short-lived pre-signed URL for a card's processed image.

    The images bucket is private, so the browser cannot fetch the picture directly. Signing it with
    the Lambda's own role lets the review page display the image inline and open it in a new tab
    without making the bucket public or proxying the bytes through this function. Any failure
    returns an empty string rather than raising, so a missing image degrades to a blank preview
    instead of breaking the whole page.

    Args:
        item: Raw DynamoDB card item, read for its finishedKey.
        expires: URL lifetime in seconds.

    Returns:
        The pre-signed GET URL, or an empty string if the card has no finishedKey, the bucket is
        not configured or signing failed.
    """
    key = _s(item, "finishedKey")
    if not key or not IMAGES_BUCKET:
        return ""
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": IMAGES_BUCKET, "Key": key},
            ExpiresIn=expires,
        )
    except Exception:
        return ""


def _finished_size_mb(item):
    """
    Reads the size of a card's processed image for display in the review UI.

    This uses HeadObject, which needs only s3:GetObject — already granted on the finished prefix —
    so showing the size costs no extra IAM permission and does not download the object. The value
    shown is the processed PNG, which is the counterpart of the byte size displayed for rejected
    uploads (those are the un-processed originals). A missing or unreadable object degrades to a
    dash rather than an error.

    Args:
        item: Raw DynamoDB card item, read for its finishedKey.

    Returns:
        The size formatted as "X.XX MB", or an em dash if it could not be determined.
    """
    key = _s(item, "finishedKey")
    if not key or not IMAGES_BUCKET:
        return "—"
    try:
        resp = s3.head_object(Bucket=IMAGES_BUCKET, Key=key)
        return f"{resp['ContentLength'] / 1048576:.2f} MB"
    except Exception:  # noqa: BLE001
        return "—"


def update_card(card_id, values):
    """
    Writes the edited fields back to a card and stamps updatedAt.

    The update expression is built dynamically from whatever fields were submitted, using
    placeholder names and values so that a field named like a DynamoDB reserved word cannot break
    the expression. Every value is stored as a DynamoDB string. A condition on
    attribute_exists(cardId) is essential rather than cosmetic: UpdateItem is an upsert, so without
    it a POST carrying a bogus cardId would silently CREATE a junk card instead of failing.

    Args:
        card_id: Identifier of the card to update.
        values: Mapping of field name to new string value.

    Returns:
        None. The fields and a fresh updatedAt timestamp are written to the card's DynamoDB item.

    Raises:
        ddb.exceptions.ConditionalCheckFailedException: If no card with that id exists.
    """
    from datetime import datetime, timezone

    names = {}
    vals = {}
    sets = []
    for i, (field, value) in enumerate(values.items()):
        names[f"#f{i}"] = field
        vals[f":v{i}"] = {"S": value}
        sets.append(f"#f{i} = :v{i}")
    names["#ua"] = "updatedAt"
    vals[":ua"] = {"S": datetime.now(timezone.utc).isoformat()}
    sets.append("#ua = :ua")

    ddb.update_item(
        TableName=TABLE,
        Key={"cardId": {"S": card_id}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        ConditionExpression="attribute_exists(cardId)",
    )


def set_publish_state(card_id, status, url="", error=""):
    """
    Records the outcome of a GitHub publish attempt on the card item.

    Without this bookkeeping a failed publish is invisible: the card sits as "approved" forever
    while never actually reaching the site. Writing the outcome makes the failure both visible in
    the review UI and retryable. Four attributes are maintained — publishStatus (pending, published
    or failed), githubPublishedUrl on success, githubPublishError on failure (truncated, because
    GitHub and HTTP error strings can be very long), and publishedAt, set only on success.
    It needs no IAM beyond the UpdateItem the editor holds for its edits, and the editor owns this
    bookkeeping because it is the component that receives the publish result synchronously — the
    committer never writes publish state. The write carries the same attribute_exists(cardId)
    condition as update_card(), so bookkeeping never creates a row.
    The whole write is best effort. A failure here must never mask the actual publish result, so it
    is caught and logged rather than raised.

    Args:
        card_id: Identifier of the card that was published.
        status: One of "pending", "published" or "failed".
        url: Commit URL, on success.
        error: Error description, on failure; truncated to 500 characters.

    Returns:
        None. The publish attributes are written to the card's DynamoDB item; a failed write is only
        printed to the log.
    """
    from datetime import datetime, timezone

    names = {"#ps": "publishStatus", "#url": "githubPublishedUrl", "#err": "githubPublishError"}
    vals = {
        ":ps": {"S": status},
        ":url": {"S": url or ""},
        ":err": {"S": (error or "")[:500]},
    }
    sets = ["#ps = :ps", "#url = :url", "#err = :err"]
    if status == "published":
        names["#pa"] = "publishedAt"
        vals[":pa"] = {"S": datetime.now(timezone.utc).isoformat()}
        sets.append("#pa = :pa")
    try:
        ddb.update_item(
            TableName=TABLE,
            Key={"cardId": {"S": card_id}},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
            ConditionExpression="attribute_exists(cardId)",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[review-editor] set_publish_state({card_id}, {status}) failed: {e}")


def _invoke_committer(action, card_id):
    """
    Synchronously invokes the internal committer Lambda, which owns the privileged operations.

    The committer holds the GitHub token and the delete power; this internet-facing editor holds
    neither, and has only lambda:InvokeFunction on that one function. The payload deliberately
    carries nothing but an action and a cardId — the committer re-reads the card from DynamoDB
    itself and, when publishing, re-verifies that its status is "approved", so nothing this
    function sends is ever trusted as content. That separation is what lets the review UI be
    exposed to the internet without exposing the credentials behind it.
    The invoke is synchronous because the caller needs the outcome to record publish state and show
    the result. Both transport failures and application-level errors are returned as strings rather
    than raised, so the caller has one uniform way to handle them.

    Args:
        action: Either "publish" or "delete".
        card_id: Identifier of the card to act on.

    Returns:
        Tuple of (payload_dict, None) on success, or (None, error_string) on failure.
    """
    if not COMMITTER_FUNCTION_NAME:
        return None, "committer not configured"

    try:
        resp = lambda_client.invoke(
            FunctionName=COMMITTER_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": action, "cardId": card_id}).encode("utf-8"),
        )
        payload = json.loads(resp["Payload"].read() or "{}")
        if resp.get("FunctionError"):
            return None, f"committer error: {payload}"
        if payload.get("error"):
            return None, payload["error"]
        return payload, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _invoke_publisher(card_id):
    """
    Publishes a card by asking the committer to commit it to GitHub.

    A thin wrapper over _invoke_committer() that sends the publish action and unwraps the response
    down to the single value handle_save() needs.

    Args:
        card_id: Identifier of the approved card to publish.

    Returns:
        Tuple of (published_url, None) on success, or (None, error_string) on failure.
    """
    payload, err = _invoke_committer("publish", card_id)
    if err:
        return None, err
    return payload.get("published_url", ""), None


def _invoke_deleter(card_id):
    """
    Removes a card everywhere by asking the committer to delete it.

    A thin wrapper over _invoke_committer() that sends the delete action, used by handle_delete().

    Args:
        card_id: Identifier of the card to remove.

    Returns:
        Tuple of (summary_dict, None) on success, or (None, error_string) on failure. The summary
        reports what was actually removed from GitHub, S3 and DynamoDB.
    """
    return _invoke_committer("delete", card_id)


def _list_rejected():
    """
    Lists the quarantined uploads straight from S3, for the Rejected tab.

    Rejected uploads never reached DynamoDB — the gate quarantined them before any record was
    created — so this tab has no table to read and must list the bucket prefix instead. Each object
    gets a short-lived pre-signed URL so the private images can be previewed in the page. The
    listing is paginated to the end, and the prefix placeholder and any debug corner overlays are
    skipped so only real rejected photos are shown.

    Args:
        None. The bucket and prefix come from module-level configuration.

    Returns:
        Tuple of (items, None) on success, where each item is a dict with key, name, url and size
        (in bytes), sorted by name, and an empty list means nothing is quarantined; or
        (None, error_string) on failure.
    """
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    try:
        items = []
        token = None

        while True:
            kw = {"Bucket": IMAGES_BUCKET, "Prefix": REJECTED_PREFIX}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                k = o["Key"]
                if k.endswith("/") or k.endswith(".keep") or k.endswith("__corners.png"):
                    continue
                items.append({
                    "key": k,
                    "name": k.split("/")[-1],
                    "url": _rejected_preview_url(k),
                    "size": o.get("Size", 0),
                })
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        items.sort(key=lambda i: i["name"])
        return items, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _rejected_preview_url(key):
    """
    Builds a short-lived pre-signed URL so a rejected upload can be previewed.

    The bucket is private, so the browser cannot fetch these directly. The URL is valid for
    REJECTED_URL_TTL seconds (one hour), long enough to look through the tab. Signing is best
    effort: a failure returns an empty string, and the Rejected tab or the delete confirmation page
    then shows the upload without its picture rather than failing.

    Args:
        key: S3 key of the rejected object.

    Returns:
        The pre-signed GET URL, or an empty string when the bucket is not configured or signing
        failed.
    """
    if not IMAGES_BUCKET:
        return ""
    try:
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": IMAGES_BUCKET, "Key": key},
            ExpiresIn=REJECTED_URL_TTL,
        )
    except Exception:  # noqa: BLE001
        return ""


def _reprocess_rejected(key):
    """
    Rescues a wrongly-rejected upload by putting it back into the processing path.

    The object is written back into the raw prefix carrying a force-process marker, and the
    quarantined copy is then removed. Landing in the raw prefix re-triggers the gate, which sees
    the marker and forwards straight to the image-processor without scoring the image again — that
    matters, because an identical image would score identically and be rejected forever otherwise.
    The result is that a genuine card the gate got wrong finally gets processed and reaches
    DynamoDB. All existing metadata, the uploader included, is kept so the rescued card keeps its
    attribution. The bytes are read and PUT rather than copied with copy_object on purpose: the
    re-submit must rewrite the metadata to add the marker, and copy_object only does that when
    MetadataDirective="REPLACE" is remembered, while a read and a PUT have no such flag to forget.
    The key is checked against the quarantine prefix, so this cannot be pointed at arbitrary
    objects. Once the PUT has succeeded the upload is already being processed, so a failure to
    remove the quarantined copy is only a warning: reporting it as a failure would invite a second
    "Put to process" and a duplicate card.

    Args:
        key: S3 key of the rejected object, which must be under the quarantine prefix.

    Returns:
        Tuple of (raw_key, None) on success; (raw_key, warning_string) when the upload was sent to
        processing but its quarantined copy could not be removed; or (None, error_string) when
        nothing was sent.
    """
    if not key or not key.startswith(REJECTED_PREFIX):
        return None, f"key must be under {REJECTED_PREFIX}"
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    raw_key = RAW_PREFIX + key[len(REJECTED_PREFIX):]
    try:
        obj = s3.get_object(Bucket=IMAGES_BUCKET, Key=key)
        body = obj["Body"].read()
        md = dict(obj.get("Metadata", {}))
        md[FORCE_PROCESS_META] = "1"
        s3.put_object(
            Bucket=IMAGES_BUCKET,
            Key=raw_key,
            Body=body,
            Metadata=md,
            ContentType=obj.get("ContentType", "image/jpeg"),
        )
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    try:
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
    except Exception as e:  # noqa: BLE001
        return raw_key, f"the rejected copy could not be removed: {e}"
    return raw_key, None


def _delete_rejected(key):
    """
    Dismisses a rejected upload by deleting it from the quarantine prefix.

    The bucket is versioned, so this writes a delete marker rather than destroying the object — a
    mistaken dismissal is still recoverable. The key is checked against the quarantine prefix, so
    this cannot be used to delete cards or raw uploads.

    Args:
        key: S3 key of the rejected object, which must be under the quarantine prefix.

    Returns:
        Tuple of (True, None) on success, or (None, error_string) on failure.
    """
    if not key or not key.startswith(REJECTED_PREFIX):
        return None, f"key must be under {REJECTED_PREFIX}"
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    try:
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
        return True, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


PAGE_CSS = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 900px;
         margin: 24px auto; padding: 0 16px; color: #111; }
  a { color: #b00; }
  .filters { margin-bottom: 16px; }
  .filters a { margin-right: 12px; text-decoration: none; }
  .filters a.active { font-weight: 700; text-decoration: underline; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border-bottom: 1px solid #ddd; text-align: left; padding: 8px;
           vertical-align: top; }
  .status-pending { color: #c60; font-weight: 700; }
  .status-approved { color: #070; font-weight: 700; }
  /* publishStatus colours: green = on GitHub, red = failed
     (re-save to retry), orange = in flight. */
  .publish-published { color: #070; font-weight: 700; }
  .publish-failed { color: #c00; font-weight: 700; }
  .publish-pending { color: #c60; font-weight: 700; }
  label { display: block; font-weight: 700; margin-top: 14px; }
  input[type=text], textarea, select { width: 100%; padding: 8px; font-size: 14px;
           border: 1px solid #bbb; border-radius: 4px; box-sizing: border-box; }
  .radios { margin-top: 4px; }
  .radios label.radio { display: inline-flex; align-items: center; gap: 6px;
           font-weight: 400; margin-top: 0; margin-right: 20px; cursor: pointer; }
  .radios input[type=radio] { width: auto; }
  textarea { min-height: 90px; font-family: inherit; }
  .draft { background: #f6f6f6; border: 1px dashed #bbb; padding: 6px 8px;
           margin-top: 4px; white-space: pre-wrap; font-size: 13px; color: #444; }
  .draft b { color: #888; font-weight: 700; }
  button { margin-top: 18px; padding: 10px 18px; font-size: 15px; font-weight: 700;
           background: #FF9900; color: #111; border: none; border-radius: 6px;
           cursor: pointer; }
  .meta { color: #888; font-size: 13px; }
  .saved { background: #e6ffe6; border: 1px solid #6c6; padding: 10px;
           border-radius: 6px; margin-bottom: 16px; }
  /* Kill switch (delete). Red to distinguish from the orange Save/primary
     action. .del-form keeps the per-row list button inline in its table cell;
     .del-btn-lg is the larger button on the confirm page. */
  .deleted { background: #ffe6e6; border: 1px solid #c66; padding: 10px;
             border-radius: 6px; margin-bottom: 16px; }
  .del-form { display: inline; margin: 0; }
  button.del-btn { margin: 0; padding: 5px 12px; font-size: 13px;
           background: #c0392b; color: #fff; border-radius: 4px; }
  button.del-btn-lg { background: #c0392b; color: #fff; }
  .warn { background: #fff4e5; border: 1px solid #e0a; border-color: #d9822b;
          padding: 12px; border-radius: 6px; margin: 14px 0; }
</style>
"""


def esc(x):
    """
    Escapes a value for safe interpolation into HTML.

    Every card field rendered into a page goes through here. The values originate from public
    uploads and from a vision model's draft text, so treating them as trusted markup would be a
    stored cross-site-scripting hole in the admin UI.

    Args:
        x: String to escape, or None.

    Returns:
        The HTML-escaped string, or an empty string if the input was None.
    """
    return html.escape(x or "")


def render_list(status_filter, saved_id=None, note=None, deleted_id=None):
    """
    Renders the main card list page.

    Builds one table row per card: its title linking to the edit form, its status, its publish state
    (so a failed publish is obvious at a glance), event and year, the size of the processed image,
    the cardId and a Delete button. The size costs one HeadObject per row, which is fine at this
    catalog's size. The Delete button is a GET form that only opens the confirmation page, where the
    CSRF-protected POST does the deleting; a JavaScript confirm would not run, because the admin
    content security policy allows no script.
    The optional arguments drive the confirmation banner shown after a redirect, so the result of a
    save or a delete is visible on the page the user lands on rather than being lost in the
    redirect. A delete that did not finish is reported without claiming what was or was not
    removed, because the committer may have stopped after GitHub and S3; running it again is safe.

    Args:
        status_filter: Status to filter by, or None for all cards.
        saved_id: Card just saved, confirmed in the banner; it takes precedence over deleted_id.
        note: Tokens separated by ";;" that fill the banner: "pub:<commit url>" and
            "gh_err:<message>" after a save, "del_err:<message>" after a delete that did not finish,
            and one "del_warn:<message>" per warning of a delete that did. Other tokens are ignored.
        deleted_id: Card just deleted, or whose delete failed, reported in the banner.

    Returns:
        Complete HTML document as a string; a table row saying "No cards." when nothing matches.
    """
    rows = []
    items = list_cards(status_filter)
    for it in items:
        cid = _s(it, "cardId")
        st = _s(it, "status")
        st_cls = "status-pending" if st == "pending" else "status-approved" if st == "approved" else ""
        ps = _s(it, "publishStatus")
        ps_cls = ("publish-published" if ps == "published"
                  else "publish-failed" if ps == "failed"
                  else "publish-pending" if ps == "pending" else "")
        title = _s(it, "title") or _s(it, "title_ai_draft") or "(untitled)"
        del_btn = (
            f"<form class='del-form' method='get' action='{BASE}/delete'>"
            f"<input type='hidden' name='cardId' value='{esc(cid)}'>"
            f"<button class='del-btn' type='submit'>Delete</button></form>"
        )
        rows.append(
            f"<tr>"
            f"<td><a href='{BASE}/edit?cardId={urllib.parse.quote(cid)}'>{esc(title)}</a></td>"
            f"<td class='{st_cls}'>{esc(st)}</td>"
            f"<td class='{ps_cls}'>{esc(ps or '—')}</td>"
            f"<td class='meta'>{esc(_s(it, 'event'))} {esc(_s(it, 'year'))}</td>"
            f"<td class='meta'>{esc(_finished_size_mb(it))}</td>"
            f"<td class='meta'>{esc(cid)}</td>"
            f"<td>{del_btn}</td>"
            f"</tr>"
        )

    banner = ""
    if saved_id:
        parts = []
        for t in (note.split(";;") if note else []):
            if t.startswith("pub:"):
                url = t[len("pub:"):]
                parts.append(
                    f"Published to GitHub: <a href='{esc(url)}' target='_blank' rel='noopener'>{esc(url)}</a>"
                )
            elif t.startswith("gh_err:"):
                parts.append(f"<b>GitHub publish failed:</b> {esc(t[len('gh_err:'):])}")
        extra = (" &middot; " + " &middot; ".join(parts)) if parts else ""
        banner = f"<div class='saved'>Saved {esc(saved_id)}.{extra}</div>"
    elif deleted_id:
        err = ""
        warnings = []
        for t in (note.split(";;") if note else []):
            if t.startswith("del_err:"):
                err = t[len("del_err:"):]
            elif t.startswith("del_warn:"):
                warnings.append(t[len("del_warn:"):])
        if err:
            banner = (
                f"<div class='deleted'><b>The delete did not finish for {esc(deleted_id)}:</b> "
                f"{esc(err)} &middot; try again.</div>"
            )
        elif warnings:
            banner = (
                f"<div class='deleted'>Deleted {esc(deleted_id)} — DynamoDB item removed, with "
                f"warnings: {' &middot; '.join(esc(w) for w in warnings)}</div>"
            )
        else:
            banner = (
                f"<div class='deleted'>Deleted {esc(deleted_id)} — DynamoDB item, S3 "
                f"images (raw + corners + finished), and any published GitHub files.</div>"
            )
    body = (
        f"<h1>BuilderCards editor</h1>"
        f"<div class='meta'>table <b>{esc(TABLE)}</b> &middot; region <b>{esc(REGION)}</b></div>"
        f"{banner}"
        f"{_filter_bar(status_filter)}"
        f"<table><tr><th>Title</th><th>Status</th><th>Publish</th><th>Event</th><th>Size</th><th>cardId</th><th>Delete</th></tr>"
        f"{''.join(rows) or '<tr><td colspan=7>No cards.</td></tr>'}</table>"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>BuilderCards editor</title>{PAGE_CSS}</head><body>{body}</body></html>"


def render_edit(card_id):
    """
    Renders the single-card edit form.

    Shows the card's keys, its processed image and every field in EDITABLE: the status as radio
    buttons, the MULTILINE fields as text areas and the rest as text inputs. A field that is still
    empty is pre-filled with its AI draft (the DRAFTS fields), because the drafts are usually right
    and only need a tweak; the draft also stays visible, read-only, below the field as a reference.
    Once a publish has been attempted, the page shows its state, and after a failure the error with
    the hint that re-saving with status approved retries it. The form carries a per-card CSRF token,
    and the approve control is what arms publishing on save. The Delete button is a separate GET
    form outside the save form, so it never submits the edits.

    Args:
        card_id: Identifier of the card to edit.

    Returns:
        Complete HTML document as a string, or a short "card not found" page if the id is unknown.
    """
    item = get_card(card_id)
    if not item:
        return f"<!doctype html><html><body><p>Card not found: {esc(card_id)}</p><a href='{BASE}'>back</a></body></html>"

    fields_html = []
    for field in EDITABLE:
        val = _s(item, field)
        draft_field = DRAFTS.get(field)
        dval = _s(item, draft_field) if draft_field else ""
        if not val and dval:
            val = dval
        label = field.capitalize()
        if field == "status":
            opts = []
            for opt in ("pending", "approved"):
                chk = " checked" if val == opt else ""
                opts.append(
                    f"<label class='radio'><input type='radio' name='status' "
                    f"value='{opt}'{chk}> {opt}</label>"
                )
            inp = f"<div class='radios'>{''.join(opts)}</div>"
        elif field in MULTILINE:
            inp = f"<textarea name='{field}'>{esc(val)}</textarea>"
        else:
            inp = f"<input type='text' name='{field}' value='{esc(val)}'>"
        draft_html = ""
        if dval:
            draft_html = f"<div class='draft'><b>AI draft:</b> {esc(dval)}</div>"
        fields_html.append(f"<label>{label}</label>{inp}{draft_html}")

    meta = (
        f"<div class='meta'>cardId <b>{esc(card_id)}</b><br>"
        f"rawKey: {esc(_s(item, 'rawKey'))}<br>"
        f"finishedKey: {esc(_s(item, 'finishedKey'))}<br>"
        f"finished image size: <b>{esc(_finished_size_mb(item))}</b><br>"
        f"created: {esc(_s(item, 'createdAt'))} &middot; updated: {esc(_s(item, 'updatedAt'))}</div>"
    )

    ps = _s(item, "publishStatus")
    if ps:
        ps_cls = ("publish-published" if ps == "published"
                  else "publish-failed" if ps == "failed" else "publish-pending")
        pub_url = _s(item, "githubPublishedUrl")
        pub_err = _s(item, "githubPublishError")
        published_at = _s(item, "publishedAt")
        line = f"publish: <b class='{ps_cls}'>{esc(ps)}</b>"
        if ps == "published" and pub_url:
            line += f" &middot; <a href='{esc(pub_url)}' target='_blank' rel='noopener'>commit</a>"
        if published_at:
            line += f" &middot; {esc(published_at)}"
        if ps == "failed":
            line += (
                f"<br><span class='publish-failed'>error: {esc(pub_err or '(none)')}</span>"
                "<br>Re-save this card (keep status = approved) to retry the publish."
            )
        publish_html = f"<div class='meta'>{line}</div>"
    else:
        publish_html = ""

    img_url = _finished_image_url(item)
    if img_url:
        img_html = (
            "<div style='margin:12px 0;'>"
            f"<a href='{esc(img_url)}' target='_blank' rel='noopener'>"
            f"<img src='{esc(img_url)}' alt='finished card' "
            "style='max-width:300px;border-radius:12px;display:block;'></a>"
            "</div>"
        )
    else:
        img_html = "<p class='meta'>(finished image not available yet)</p>"

    body = (
        f"<p><a href='{BASE}'>&larr; back to list</a></p>"
        f"<h1>Edit card</h1>{meta}{publish_html}"
        f"{img_html}"
        f"<form method='post' action='{BASE}/save'>"
        f"<input type='hidden' name='cardId' value='{esc(card_id)}'>"
        f"<input type='hidden' name='csrf' value='{esc(_csrf_token(card_id))}'>"
        f"{''.join(fields_html)}"
        f"<br><button type='submit'>Save to DynamoDB</button>"
        f"</form>"
        f"<form class='del-form' method='get' action='{BASE}/delete' style='display:block; margin-top:10px;'>"
        f"<input type='hidden' name='cardId' value='{esc(card_id)}'>"
        f"<button class='del-btn del-btn-lg' type='submit'>Delete this card…</button>"
        f"</form>"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>Edit {esc(card_id)}</title>{PAGE_CSS}</head><body>{body}</body></html>"


def render_delete_confirm(card_id):
    """
    Renders the confirmation page of the card kill switch, reached by the GET Delete buttons.

    The page names the DynamoDB item and the S3 images the committer will remove, names the card's
    GitHub files too whenever it has a slug, and holds the CSRF-protected POST form that performs
    the delete. The GitHub line does not depend on publishStatus, because the committer deletes the
    files of any card with a slug, and a card whose republish failed may still be live from an
    earlier publish. It is a page rather than a JavaScript confirm because the admin content
    security policy allows no script.

    Args:
        card_id: Identifier of the card queued for deletion.

    Returns:
        Complete HTML document as a string, or a short "card not found" page if the id is unknown.
    """
    item = get_card(card_id)
    if not item:
        return f"<!doctype html><html><head>{PAGE_CSS}</head><body><p>Card not found: {esc(card_id)}</p><a href='{BASE}'>back</a></body></html>"

    title = _s(item, "title") or _s(item, "title_ai_draft") or "(untitled)"
    status = _s(item, "status")
    ps = _s(item, "publishStatus")
    slug = _s(item, "slug")
    raw_key = _s(item, "rawKey")
    finished_key = _s(item, "finishedKey")

    gh_line = (
        f"<li>GitHub: <code>{esc(slug)}.md</code> + <code>{esc(card_id)}.png</code> "
        f"on the live branch, if a publish put them there (this takes the card OFF the site)</li>"
        if slug
        else "<li>GitHub: nothing (card was never published)</li>"
    )
    img_url = _finished_image_url(item)
    img_html = (
        f"<div style='margin:12px 0;'><img src='{esc(img_url)}' alt='finished card' "
        "style='max-width:220px;border-radius:12px;display:block;'></div>"
        if img_url else ""
    )

    body = (
        f"<p><a href='{BASE}'>&larr; back to list</a></p>"
        f"<h1>Delete card?</h1>"
        f"{img_html}"
        f"<div class='meta'>cardId <b>{esc(card_id)}</b> &middot; title <b>{esc(title)}</b> &middot; "
        f"status <b>{esc(status)}</b> &middot; publish <b>{esc(ps or '—')}</b></div>"
        f"<div class='warn'><b>This permanently removes the card.</b> The following will be deleted:"
        f"<ul>"
        f"<li>DynamoDB item <code>{esc(card_id)}</code></li>"
        f"<li>S3 finished image <code>{esc(finished_key or ('images/finished/' + card_id + '.png'))}</code></li>"
        f"<li>S3 raw upload <code>{esc(raw_key or '(none)')}</code> + its <code>__corners.png</code> overlay</li>"
        f"{gh_line}"
        f"</ul>"
        f"S3 objects are soft-deleted (the bucket is versioned, so they are recoverable); "
        f"the DynamoDB item is not.</div>"
        f"<form method='post' action='{BASE}/delete'>"
        f"<input type='hidden' name='cardId' value='{esc(card_id)}'>"
        f"<input type='hidden' name='csrf' value='{esc(_csrf_token(card_id))}'>"
        f"<button class='del-btn-lg' type='submit'>Yes, delete permanently</button>"
        f"</form>"
        f"<p style='margin-top:10px;'><a href='{BASE}'>Cancel</a></p>"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>Delete {esc(card_id)}</title>{PAGE_CSS}</head><body>{body}</body></html>"


def _filter_bar(active):
    """
    Builds the status tab bar shared by the card list and the Rejected view.

    Both views render this same bar so the tabs stay identical, even though one is backed by
    DynamoDB and the other by an S3 listing — the user should not be able to tell that the Rejected
    tab comes from somewhere else entirely.

    Args:
        active: The currently selected filter — "pending", "approved", "rejected" or None for all.

    Returns:
        The tab bar as an HTML fragment.
    """
    def fa(val, label):
        """
        Renders one tab anchor, marked active when it matches the current filter.

        Args:
            val: Status value this tab selects, or a falsy value for the "all" tab.
            label: Visible tab text.

        Returns:
            The anchor as an HTML fragment.
        """
        cls = "active" if active == val else ""
        href = f"{BASE}?status={val}" if val else BASE
        return f"<a class='{cls}' href='{href}'>{label}</a>"
    return (
        f"<div class='filters'>{fa('pending', 'Pending')} {fa('approved', 'Approved')} "
        f"{fa(None, 'All')} {fa('rejected', 'Rejected')}</div>"
    )


def render_rejected(note=None, reprocessed=None, deleted=None):
    """
    Renders the Rejected tab: one tile per quarantined upload, read straight from S3.

    A rejected upload has no DynamoDB item, so there are no card details to show, only the picture,
    its file name and size, and two buttons. "Put to process" is a one-click POST, because it moves
    the upload rather than destroying it: the upload goes back into the processing path and
    reappears as a pending card, which is the first time it enters DynamoDB. "Delete" opens a
    confirmation page first, like the card kill switch. The optional arguments come from the
    redirect after either action and fill the result banner.

    Args:
        note: Tokens separated by ";;": "reproc_err:<message>" or "reject_del_err:<message>" after a
            failed action, "reproc_warn:<message>" after a put to process that left the rejected
            copy behind. Other tokens are ignored.
        reprocessed: Key of the upload just sent back for processing, if any.
        deleted: Key of the upload just deleted, if any.

    Returns:
        Complete HTML document as a string; it says "No rejected uploads." when the prefix is empty,
        or shows the error when the listing failed.
    """
    items, err = _list_rejected()

    banner = ""
    if reprocessed:
        banner = (
            f"<div class='saved'>Sent to processing: <b>{esc(reprocessed.split('/')[-1])}</b>. "
            f"It will reappear as a <b>pending</b> card once the processor finishes — "
            f"check the Pending tab shortly.</div>"
        )
    elif deleted:
        banner = f"<div class='deleted'>Deleted rejected upload <b>{esc(deleted.split('/')[-1])}</b>.</div>"
    if note:
        for t in note.split(";;"):
            if t.startswith("reproc_err:"):
                banner = f"<div class='deleted'><b>Put to process failed:</b> {esc(t[len('reproc_err:'):])}</div>"
            elif t.startswith("reject_del_err:"):
                banner = f"<div class='deleted'><b>Delete failed:</b> {esc(t[len('reject_del_err:'):])}</div>"
            elif t.startswith("reproc_warn:"):
                banner += (
                    f"<div class='warn'><b>Warning:</b> {esc(t[len('reproc_warn:'):])}. It is already "
                    f"being processed: delete it here rather than putting it to process again.</div>"
                )

    if err:
        cards_html = f"<div class='warn'>Could not list rejected uploads: {esc(err)}</div>"
    else:
        if not items:
            cards_html = "<p class='meta'>No rejected uploads.</p>"
        else:
            tiles = []
            for it in items:
                key = it.get("key", "")
                name = it.get("name", key)
                url = it.get("url", "")
                size_mb = f"{it.get('size', 0) / 1048576:.2f} MB"
                csrf = _csrf_token(key)
                img = (
                    f"<img src='{esc(url)}' alt='{esc(name)}' style='width:100%;height:200px;"
                    f"object-fit:contain;border-radius:8px;background:#f6f6f6;'>"
                    if url else "<div class='meta'>(preview unavailable)</div>"
                )
                reproc = (
                    f"<form method='post' action='{BASE}/reprocess' style='display:inline;margin:0;'>"
                    f"<input type='hidden' name='key' value='{esc(key)}'>"
                    f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
                    f"<button type='submit'>Put to process</button></form>"
                )
                delete = (
                    f"<form class='del-form' method='get' action='{BASE}/reject-delete' "
                    f"style='display:inline;margin-left:8px;'>"
                    f"<input type='hidden' name='key' value='{esc(key)}'>"
                    f"<button class='del-btn' type='submit'>Delete</button></form>"
                )
                tiles.append(
                    f"<div class='card-item' style='text-align:center;'>{img}"
                    f"<div class='meta' style='word-break:break-all;margin:6px 0;'>{esc(name)}"
                    f"<br><b>{esc(size_mb)}</b></div>"
                    f"<div>{reproc}{delete}</div></div>"
                )
            cards_html = (
                "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));"
                "gap:20px;margin-top:20px;'>" + "".join(tiles) + "</div>"
            )

    body = (
        f"<h1>BuilderCards editor</h1>"
        f"<div class='meta'>rejected uploads (S3 <b>{esc(REJECTED_PREFIX)}</b>) &middot; "
        f"not in DynamoDB until reprocessed</div>"
        f"{banner}"
        f"{_filter_bar('rejected')}"
        f"{cards_html}"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>Rejected uploads</title>{PAGE_CSS}</head><body>{body}</body></html>"


def render_reject_delete_confirm(key):
    """
    Renders the confirmation page for deleting a rejected upload.

    Like the card kill switch, it is a page rather than a JavaScript confirm, because the admin
    content security policy allows no script. It shows the picture, its key and the
    CSRF-protected POST form that performs the delete.

    Args:
        key: S3 key of the rejected upload queued for deletion.

    Returns:
        Complete HTML document as a string, or a short "Invalid key." page when the key is empty or
        outside the quarantine prefix.
    """
    if not key or not key.startswith(REJECTED_PREFIX):
        return (
            f"<!doctype html><html><head>{PAGE_CSS}</head><body><p>Invalid key.</p>"
            f"<a href='{BASE}?status=rejected'>back</a></body></html>"
        )
    url = _rejected_preview_url(key)
    name = key.split("/")[-1]
    img_html = (
        f"<div style='margin:12px 0;'><img src='{esc(url)}' alt='{esc(name)}' "
        "style='max-width:220px;border-radius:12px;display:block;'></div>" if url else ""
    )
    body = (
        f"<p><a href='{BASE}?status=rejected'>&larr; back to rejected</a></p>"
        f"<h1>Delete rejected upload?</h1>"
        f"{img_html}"
        f"<div class='meta'>{esc(name)}</div>"
        f"<div class='warn'><b>This deletes the rejected upload from S3.</b> "
        f"<code>{esc(key)}</code><br>The bucket is versioned, so it is a soft delete "
        f"(recoverable). No DynamoDB item exists for a rejected upload.</div>"
        f"<form method='post' action='{BASE}/reject-delete'>"
        f"<input type='hidden' name='key' value='{esc(key)}'>"
        f"<input type='hidden' name='csrf' value='{esc(_csrf_token(key))}'>"
        f"<button class='del-btn-lg' type='submit'>Yes, delete permanently</button>"
        f"</form>"
        f"<p style='margin-top:10px;'><a href='{BASE}?status=rejected'>Cancel</a></p>"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>Delete rejected</title>{PAGE_CSS}</head><body>{body}</body></html>"


def _html_resp(body, code=200):
    """
    Wraps an HTML body in a response carrying the admin security headers.

    Every HTML page this Lambda returns past the auth check goes through here (the 401 and 503
    responses of the auth check do not), so the whole header set is defined in one place and
    cannot drift between routes. Caching is disabled because these pages show
    pending review data; framing is denied because a page with an Approve button is a real
    clickjacking target; the referrer is suppressed so the admin URL and cardId do not leak to
    outbound card links; and the content security policy is tight enough to block script entirely,
    which it can afford to be because the UI uses no JavaScript at all — only an inline style
    block, same-origin forms and pre-signed S3 images.

    Args:
        body: Complete HTML document as a string.
        code: HTTP status code.

    Returns:
        API Gateway response dict with the HTML body and security headers.
    """
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; img-src https://*.amazonaws.com; "
                "style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
                "frame-ancestors 'none'"
            ),
        },
        "body": body,
    }


def _redirect(location, code=303):
    """
    Builds a redirect response, used after every state-changing POST.

    Redirecting after a write is what stops a browser refresh from re-submitting the form and
    repeating the action — the post/redirect/get pattern. The default 303 makes the follow-up
    request a GET regardless of the original method.

    Args:
        location: URL to redirect to.
        code: HTTP status code; 303 by default.

    Returns:
        API Gateway response dict with the Location header set.
    """
    return {
        "statusCode": code,
        "headers": {
            "Location": location,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
        "body": "",
    }


def _read_body(event):
    """
    Extracts the request body from an API Gateway event, decoding it if needed.

    API Gateway may deliver the body base64-encoded depending on the content type, so the flag has
    to be honoured rather than assumed — reading it raw would corrupt form submissions.

    Args:
        event: API Gateway HTTP API payload v2.0 event.

    Returns:
        The body as a decoded string, or an empty string if there was none.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return raw


def _get_credentials():
    """
    Fetches the Basic Auth credentials from Secrets Manager, with a bounded cache.

    The credentials are cached per warm container (in _credentials, with the time.monotonic() of the
    last successful fetch in _credentials_fetched_at) for CREDS_TTL_SECONDS, five minutes, rather
    than for the container's whole life. That bounds how long a rotated secret keeps being honoured
    — an unbounded cache would let a revoked password work until the container happened to recycle
    — while the extra GetSecretValue, once per TTL per warm container, is negligible at this
    traffic.
    The failure behaviour is deliberately asymmetric. On a cold cache a fetch failure propagates,
    so the caller denies access and the endpoint fails CLOSED: an unreadable secret must never mean
    "let everyone in". On a refresh failure, where valid credentials are already cached, those are
    kept and the fetch is retried next time, so a transient Secrets Manager blip does not knock out
    a working admin session.

    Args:
        None. The secret ARN comes from module-level configuration.

    Returns:
        Dict with "username" and "password".

    Raises:
        Exception: If the secret cannot be read or parsed and nothing is cached yet.
    """
    global _credentials, _credentials_fetched_at
    now = time.monotonic()
    if _credentials is not None and (now - _credentials_fetched_at) < CREDS_TTL_SECONDS:
        return _credentials
    try:
        resp = secretsmanager.get_secret_value(SecretId=BASIC_AUTH_SECRET_ARN)
        data = json.loads(resp["SecretString"])
        _credentials = {"username": data["username"], "password": data["password"]}
        _credentials_fetched_at = now
        return _credentials
    except Exception:
        if _credentials is not None:
            return _credentials
        raise


def _unauthorized():
    """
    Builds the 401 response that triggers the browser's native login prompt.

    The WWW-Authenticate header is what makes the browser show its own credential dialog, so the
    admin UI needs no login page of its own.

    Args:
        None.

    Returns:
        API Gateway 401 response dict with the WWW-Authenticate challenge.
    """
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "WWW-Authenticate": 'Basic realm="BuilderCards review"',
        },
        "body": "<p>Authentication required.</p>",
    }


def _authorize(event):
    """
    Checks a request's Basic Auth credentials, gating every route in this Lambda.

    Both the username and the password are compared with hmac.compare_digest rather than ==, so
    neither can be recovered by measuring how long the comparison takes. Every failure path denies:
    an unreadable secret, a missing or malformed Authorization header, bad base64, or a mismatch —
    this fails CLOSED by construction, with the only successful return being an explicit match on
    both fields.

    Args:
        event: API Gateway HTTP API payload v2.0 event, read for its authorization header.

    Returns:
        None when the credentials are valid, otherwise the error response the caller should return
        immediately — a 401 challenge, or a 503 when no usable secret is configured.
    """
    try:
        creds = _get_credentials()
    except Exception as e:
        print(f"[review-editor] basic-auth secret unavailable: {e}")
        return {
            "statusCode": 503,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": "Auth not configured.",
        }

    auth = (event.get("headers") or {}).get("authorization") or ""
    if auth[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1], validate=True).decode("utf-8")
            user, _, pwd = decoded.partition(":")
            user_ok = hmac.compare_digest(user.encode("utf-8"), creds["username"].encode("utf-8"))
            pwd_ok = hmac.compare_digest(pwd.encode("utf-8"), creds["password"].encode("utf-8"))
            if user_ok and pwd_ok:
                return None
        except Exception:
            pass
    return _unauthorized()


def _csrf_token(card_id):
    """
    Derives a per-card CSRF token by HMAC-ing the card id with the auth password.

    This is stateless by design — there is no session store, and the token can be recomputed on
    demand from something only the server knows. It closes a real hole: browsers cache Basic Auth
    credentials and attach them automatically, so without a token any site could POST to the save
    or delete routes and have it succeed. An attacker cannot forge the token, because they do not
    have the password, and cannot read it, because the edit page is opaque to them cross-origin.
    Binding it to the card id means a token captured for one card is useless against another.

    Args:
        card_id: The card the token is bound to; also used for rejected-upload keys.

    Returns:
        The token as a hex-encoded HMAC-SHA256 digest.

    Raises:
        Exception: If the credentials cannot be fetched (passed on from _get_credentials()).
    """
    key = _get_credentials()["password"].encode("utf-8")
    return hmac.new(key, card_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _csrf_ok(card_id, token):
    """
    Verifies a submitted CSRF token against the expected one for a card.

    The comparison is constant time, and any failure to even compute the expected token — for
    instance if the credentials are unavailable — is treated as a rejection rather than an error,
    so this fails closed.

    Args:
        card_id: The card the token should be bound to.
        token: Token submitted with the form, possibly missing.

    Returns:
        True only if the token matches; False on mismatch, absence or any error.
    """
    try:
        return hmac.compare_digest(token or "", _csrf_token(card_id))
    except Exception:
        return False


SLUG_MAX_LEN = 100
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _validate(values):
    """
    Checks the submitted form values and reports the first problem.

    The form constrains these in the UI, but a crafted POST need not, so every field is re-checked
    here — the slug most of all. The slug is a path component, not free text: the committer puts
    it straight into the GitHub commit path (<content dir>/<slug>.md), and it becomes the public
    URL segment (/collectibles/<slug>/). A slug holding "/" or ".." would commit a file outside the
    content directory on the live branch that Amplify builds, which is build-time code execution.
    It is checked here although the route is already behind Basic Auth and CSRF, so that one leaked
    credential or one careless paste cannot reach amplify.yml. URL quoting is no protection, because
    urllib.parse.quote() leaves "/" and ".." as they are.

    SLUG_RE accepts exactly what the image-processor's slugify() produces: lowercase alphanumeric
    groups joined by single hyphens, a form that cannot hold "/", "\\", "..", a leading dot,
    whitespace or control characters. It is applied with fullmatch(), not with match() and "^...$",
    because "$" also matches just before a trailing newline, so "aws-card\\n" would pass.
    SLUG_MAX_LEN keeps <slug>.md a valid file name (file systems cap a name at 255 bytes). The
    checks run on the slug with surrounding whitespace stripped. The format is checked whenever
    that slug is non-empty, not only on approve, so a pending card cannot store a slug holding "/"
    or ".." either: the committer's delete path builds GitHub paths from the stored slug whatever
    the card's status.

    Args:
        values: Mapping of submitted field names to their string values.

    Returns:
        An error message describing the first problem found, or an empty string if all values are
        acceptable.
    """
    status = values.get("status", "")
    if status not in ("pending", "approved"):
        return "status must be 'pending' or 'approved'."
    for numfield in ("year", "weight"):
        v = (values.get(numfield, "") or "").strip()
        if v and not v.isdigit():
            return f"{numfield} must be a whole number (or empty)."
    slug = (values.get("slug", "") or "").strip()
    if slug and (len(slug) > SLUG_MAX_LEN or not SLUG_RE.fullmatch(slug)):
        return (
            "slug must be lowercase a-z / 0-9 joined by SINGLE hyphens, e.g. "
            f"'aws-summit-hamburg-2025' (max {SLUG_MAX_LEN} chars). No dots, "
            "slashes, spaces, underscores, uppercase, or leading/trailing hyphens."
        )
    if status == "approved":
        if not (values.get("title", "") or "").strip():
            return "title is required to approve."
        if not slug:
            return "slug is required to approve."
    return ""


def _empty_slug_problem(item):
    """
    Reports why the stored card's slug may not be emptied, if it may not.

    The stored slug is the only record of where a published card's page is: the committer's delete
    removes <content dir>/<slug>.md by it. Emptying it on a card that a publish was ever attempted for
    (any publishStatus) would leave that page on the site after a delete, so such a card keeps its
    slug, and taking it off the site means deleting the card.

    Args:
        item: The stored DynamoDB item of the card, or None if it does not exist.

    Returns:
        An error message when the stored card has both a slug and a publishStatus; an empty string
        when its slug may be emptied, or when the card does not exist (the save then reports that).
    """
    if item and _s(item, "slug") and _s(item, "publishStatus"):
        return (
            "this card has been published (or a publish was attempted), and its slug is what lets a "
            "delete take its page off the site, so the slug cannot be emptied. To take the card off "
            "the site, delete it."
        )
    return ""


def handle_save(event):
    """
    Handles the edit form POST: validates, saves, and publishes when approved.

    This is the human approval gate, and the only route that can put a card on the public site.
    Nothing is written without a valid CSRF token and values that pass _validate(). Every field is
    stripped of surrounding whitespace first, so the value saved is the value checked. Every save
    while the status is approved republishes, so editing a published card (fixing a typo, say)
    overwrites its files on the live branch: the markdown is keyed by the slug and the image by the
    stable cardId, so the same card lands on the same paths. A changed slug leaves the old
    <slug>.md behind, and a card that a publish was ever attempted for cannot have its slug emptied
    (_empty_slug_problem()). The same rule covers the first approval and a retry after a failed publish.
    The publish state is set to pending before the committer is invoked, so a crash before the
    result shows as pending instead of silently keeping the old state, and the outcome is recorded
    either way, so a failure is visible and retryable rather than leaving the card silently
    approved-but-unpublished.
    Returns a redirect rather than a page, so refreshing the browser cannot re-submit the form.

    Args:
        event: API Gateway event carrying the URL-encoded form body.

    Returns:
        A redirect response back to the list, carrying the publish result as notes, on success; or
        an HTML error response on a missing cardId (400), a rejected CSRF token (403), a validation
        failure (400) or an unknown card (404).
    """

    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    card_id = form.get("cardId", [""])[0]
    if not card_id:
        return _html_resp("<p>Missing cardId</p>", 400)

    if not _csrf_ok(card_id, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the edit page and try again.</p>"
            f"<a href='{BASE}/edit?cardId={urllib.parse.quote(card_id)}'>back</a>",
            403,
        )

    values = {f: form.get(f, [""])[0].strip() for f in EDITABLE}

    problem = _validate(values)
    if not problem and not values.get("slug"):
        problem = _empty_slug_problem(get_card(card_id))
    if problem:
        return _html_resp(
            f"<p>Invalid input: {esc(problem)}</p>"
            f"<a href='{BASE}/edit?cardId={urllib.parse.quote(card_id)}'>back</a>",
            400,
        )

    new_status = values.get("status", "")

    try:
        update_card(card_id, values)
    except ddb.exceptions.ConditionalCheckFailedException:
        return _html_resp(
            f"<p>Card {esc(card_id)} does not exist (refusing to create a new item).</p>"
            f"<a href='{BASE}'>back</a>",
            404,
        )

    notes = []
    should_publish = new_status == "approved"
    if should_publish:
        set_publish_state(card_id, "pending")
        published_url, gh_err = _invoke_publisher(card_id)
        if gh_err:
            set_publish_state(card_id, "failed", error=gh_err)
            notes.append(f"gh_err:{gh_err}")
        else:
            set_publish_state(card_id, "published", url=(published_url or ""))
            if published_url:
                notes.append(f"pub:{published_url}")

    loc = f"{BASE}?saved={urllib.parse.quote(card_id)}"
    if notes:
        loc += f"&note={urllib.parse.quote(';;'.join(notes))}"
    return _redirect(loc)


def handle_delete(event):
    """
    Handles the kill switch POST: has the committer remove a card everywhere.

    The CSRF token comes from the confirmation page, which only an authenticated user can load, so
    a cross-site POST riding on cached Basic Auth credentials cannot forge it. The committer removes
    the published GitHub files first and stops if that fails, then the S3 objects, and the DynamoDB
    item last, so a failed delete normally leaves the card in the list to retry.

    Args:
        event: API Gateway event carrying the form body with cardId and CSRF token.

    Returns:
        A redirect back to the list, with a del_err note when the committer reported an error, or a
        del_warn note for each warning of a finished delete (a skipped GitHub step, S3 objects not
        removed); or an HTML error response on a missing cardId (400) or a rejected CSRF token
        (403).
    """
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    card_id = form.get("cardId", [""])[0]
    if not card_id:
        return _html_resp("<p>Missing cardId</p>", 400)

    if not _csrf_ok(card_id, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the delete page and try again.</p>"
            f"<a href='{BASE}/delete?cardId={urllib.parse.quote(card_id)}'>back</a>",
            403,
        )

    summary, err = _invoke_deleter(card_id)
    loc = f"{BASE}?deleted={urllib.parse.quote(card_id)}"
    if err:
        loc += f"&note={urllib.parse.quote('del_err:' + err)}"
    else:
        warnings = list(summary.get("s3_warnings", []))
        if summary.get("github_skipped"):
            warnings.insert(0, f"GitHub step skipped ({summary['github_skipped']}): check the live "
                               f"branch for a page left behind")
        if warnings:
            loc += f"&note={urllib.parse.quote(';;'.join('del_warn:' + w for w in warnings))}"
    return _redirect(loc)


def handle_reprocess(event):
    """
    Handles the "Put to process" POST of the Rejected tab.

    _reprocess_rejected() moves the upload back into the raw prefix with the force-process marker,
    so it flows through the normal pipeline and finally lands in DynamoDB. A rejected upload has no
    cardId yet, so its CSRF token is bound to its S3 key.

    Args:
        event: API Gateway event carrying the form body with the S3 key and CSRF token.

    Returns:
        A redirect back to the Rejected tab with a result banner; or an HTML error response on a
        missing key (400) or a rejected CSRF token (403).
    """
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    key = form.get("key", [""])[0]
    if not key:
        return _html_resp("<p>Missing key</p>", 400)
    if not _csrf_ok(key, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the rejected page and try again.</p>"
            f"<a href='{BASE}?status=rejected'>back</a>", 403,
        )
    raw_key, err = _reprocess_rejected(key)
    loc = f"{BASE}?status=rejected"
    if not raw_key:
        loc += f"&note={urllib.parse.quote('reproc_err:' + err)}"
    else:
        loc += f"&reprocessed={urllib.parse.quote(key)}"
        if err:
            loc += f"&note={urllib.parse.quote('reproc_warn:' + err)}"
    return _redirect(loc)


def handle_reject_delete(event):
    """
    Handles the delete POST of the Rejected tab.

    _delete_rejected() deletes the object from the quarantine prefix, a soft delete on the
    versioned bucket. As for reprocessing, the CSRF token is bound to the S3 key.

    Args:
        event: API Gateway event carrying the form body with the S3 key and CSRF token.

    Returns:
        A redirect back to the Rejected tab with a result banner; or an HTML error response on a
        missing key (400) or a rejected CSRF token (403).
    """
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    key = form.get("key", [""])[0]
    if not key:
        return _html_resp("<p>Missing key</p>", 400)
    if not _csrf_ok(key, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the rejected page and try again.</p>"
            f"<a href='{BASE}?status=rejected'>back</a>", 403,
        )
    _ok, err = _delete_rejected(key)
    loc = f"{BASE}?status=rejected"
    if err:
        loc += f"&note={urllib.parse.quote('reject_del_err:' + err)}"
    else:
        loc += f"&reject_deleted={urllib.parse.quote(key)}"
    return _redirect(loc)


def handler(event, context):
    """
    Lambda entry point: authenticates the request, then routes it to a view or an action.

    Authorization runs before any routing at all, so every page and every action is gated by the
    same check and no route can be reached unauthenticated — the public upload endpoint is a
    separate Lambda and is unaffected. After that the request is dispatched by method and path:
    GETs render the list, the edit form, the rejected tab or a confirmation page, while POSTs run
    the state-changing actions, each of which verifies its own CSRF token. A favicon request gets
    an empty 204, and any other method and path a 404 page. An exception from AWS or the code is
    shown in the browser as a 500 page with its message, so the maintainer sees what failed.

    Args:
        event: API Gateway HTTP API payload v2.0 event.
        context: Lambda context object, unused.

    Returns:
        An API Gateway response dict: an HTML page, a redirect, an empty 204 for the favicon, or
        the 401 or 503 response of the auth check.
    """

    denied = _authorize(event)
    if denied is not None:
        return denied

    rc = event.get("requestContext", {}).get("http", {})
    method = rc.get("method", "GET")
    path = rc.get("path") or event.get("rawPath") or BASE
    qs = event.get("queryStringParameters") or {}

    try:
        if method == "GET" and path in (BASE, BASE + "/"):
            if qs.get("status") == "rejected":
                return _html_resp(render_rejected(
                    note=qs.get("note"),
                    reprocessed=qs.get("reprocessed"),
                    deleted=qs.get("reject_deleted"),
                ))
            return _html_resp(
                render_list(qs.get("status"), saved_id=qs.get("saved"),
                            note=qs.get("note"), deleted_id=qs.get("deleted"))
            )
        if method == "GET" and path == BASE + "/edit":
            return _html_resp(render_edit(qs.get("cardId", "")))
        if method == "POST" and path == BASE + "/save":
            return handle_save(event)
        if method == "GET" and path == BASE + "/delete":
            return _html_resp(render_delete_confirm(qs.get("cardId", "")))
        if method == "POST" and path == BASE + "/delete":
            return handle_delete(event)
        if method == "POST" and path == BASE + "/reprocess":
            return handle_reprocess(event)
        if method == "GET" and path == BASE + "/reject-delete":
            return _html_resp(render_reject_delete_confirm(qs.get("key", "")))
        if method == "POST" and path == BASE + "/reject-delete":
            return handle_reject_delete(event)
        if path.endswith("/favicon.ico"):
            return {"statusCode": 204, "headers": {}, "body": ""}
        return _html_resp(f"<p>Not found</p><a href='{BASE}'>back</a>", 404)
    except Exception as e:
        return _html_resp(f"<pre>Error: {esc(str(e))}</pre><a href='{BASE}'>back</a>", 500)
