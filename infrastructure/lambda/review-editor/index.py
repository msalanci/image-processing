# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Michal Salanci

"""The one page in this pipeline a human touches: review a card, then approve it.

Serves server-rendered HTML behind its own admin API. 
Each card is shown with a preview of its finished image and the model's drafts pre-filled into editable fields; 
saving a card whose status is "approved" asks the committer lambda to publish it. 
A Rejected tab lists the quarantined uploads straight from S3, since those never got a database record.

Functions:
    handler: Entry point; authorises the request and routes it.
    handle_save: Saves edits and publishes the card when it is approved.
    handle_delete: Asks the committer to remove a card everywhere.

Environment (set by Terraform):
    DDB_TABLE               card table (default BuilderCards)
    BASE_PATH               prefix the routes live under (default /review)
    BASIC_AUTH_SECRET_ARN   Secrets Manager ARN of the {username, password} JSON
    IMAGES_BUCKET           private bucket the image previews are signed against
    COMMITTER_FUNCTION_NAME the only function this editor may invoke
    REJECTED_PREFIX         quarantine prefix the Rejected tab lists
    RAW_PREFIX              prefix a re-submitted image is put back into
    AWS_REGION              provided automatically by the Lambda runtime
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
import urllib.request
import urllib.error

import boto3

TABLE = os.environ.get("DDB_TABLE", "BuilderCards")
REGION = os.environ.get("AWS_REGION", "eu-central-1")
BASE = os.environ.get("BASE_PATH", "/review")
BASIC_AUTH_SECRET_ARN = os.environ.get("BASIC_AUTH_SECRET_ARN", "")
IMAGES_BUCKET = os.environ.get("IMAGES_BUCKET", "")
COMMITTER_FUNCTION_NAME = os.environ.get("COMMITTER_FUNCTION_NAME", "")
REJECTED_PREFIX = os.environ.get("REJECTED_PREFIX", "images/rejected/")
RAW_PREFIX = os.environ.get("RAW_PREFIX", "images/raw/")
FORCE_PROCESS_META = "force-process"
REJECTED_URL_TTL = 3600

ddb = boto3.client("dynamodb", region_name=REGION)
secretsmanager = boto3.client("secretsmanager", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)  # invokes the committer

# Credentials time for credentials
CREDS_TTL_SECONDS = 300
_credentials = None  # cached {"username","password"}
_credentials_fetched_at = 0.0

# Editable fields in the form - stored in DynamoDB
EDITABLE = ["title", "slug", "weight", "event", "year", "effect", "description", "category", "subcategory", "uploader", "status"]

# AI draft fields shown read-only next to their editable counterpart
DRAFTS = {
    "title": "title_ai_draft",
    "slug": "slug_ai_draft",
    "effect": "effect_ai_draft",
    "description": "description_ai_draft",
}

# Long text fields render as <textarea>, the rest as <input>
MULTILINE = {"effect", "description"}


def _s(item, key):
    """
    Reads a string attribute out of a raw DynamoDB item.

    The low-level client wraps every value in a type descriptor, so a string lives at item[key]["S"]. 
    A missing item or attribute gives an empty string, not an error.

    Args:
        item: Raw DynamoDB item dict, or None.
        key: Attribute name to read.

    Returns:
        The attribute's string value, or an empty string if missing.
    """
    return item.get(key, {}).get("S", "") if item else ""


def list_cards(status_filter=None):
    """
    Fetches every card, optionally narrowed to one status.

    A status query reads only its own partition of the index; without one it scans the table, which is fine at this size. 
    Both paths page to the end, so nothing is silently truncated.

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
    Builds a short-lived pre-signed URL for a card's finished image.

    Signing it lets the page show the image without the bucket being public or the bytes passing through this function. 
    A failure gives an empty string, so a missing image is a blank preview rather than a broken page.

    Args:
        item: Raw DynamoDB card item, read for its finishedKey.
        expires: URL lifetime in seconds.

    Returns:
        The pre-signed GET URL, or an empty string if there is no image or signing failed.
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
    Reads the size of a card's finished image, for the review list.

    HeadObject needs no permission beyond the read already granted, and does not download the object. 
    Anything unreadable shows as a dash.

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
    except Exception:  # noqa: BLE001 - missing/unreadable -> just show a dash
        return "—"


def update_card(card_id, values):
    """
    Writes the edited fields back to a card and stamps updatedAt.

    UpdateItem is an upsert, so the condition matters: without it a post carrying 
    a bogus cardId would quietly create a junk card instead of failing.

    Args:
        card_id: Identifier of the card to update.
        values: Mapping of field name to new string value.

    Returns:
        None.

    Raises:
        ConditionalCheckFailedException: If no card with that id exists.
    """
    from datetime import datetime, timezone

    # Build SET clauses from placeholders, so reserved words cannot break the expression
    names = {}
    vals = {}
    sets = []
    for i, (field, value) in enumerate(values.items()):
        names[f"#f{i}"] = field
        vals[f":v{i}"] = {"S": value}
        sets.append(f"#f{i} = :v{i}")

    # Always bump the edit timestamp alongside the submitted fields
    names["#ua"] = "updatedAt"
    vals[":ua"] = {"S": datetime.now(timezone.utc).isoformat()}
    sets.append("#ua = :ua")

    ddb.update_item(
        TableName=TABLE,
        Key={"cardId": {"S": card_id}},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        
        # Refuse to CREATE a new item
        ConditionExpression="attribute_exists(cardId)",
    )


def set_publish_state(card_id, status, url="", error=""):
    """
    Records the outcome of a publish attempt on the card item.

    Without this a failed publish is invisible: the card reads "approved" forever while never reaching the site. 
    publishedAt is written only on success, so that field never lies. 
    The whole write is best effort, because it must never mask the publish result it is reporting.

    Args:
        card_id: Identifier of the card that was published.
        status: One of "pending", "published" or "failed".
        url: Commit URL, on success.
        error: Error description, on failure; truncated to 500 characters.

    Returns:
        None.
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
    except Exception as e:
        print(f"[review-editor] set_publish_state({card_id}, {status}) failed: {e}")


def _invoke_committer(action, card_id):
    """
    Invokes the committer, which owns the GitHub token and the delete power.

    The payload is two strings and nothing else: the committer re-reads the card itself and re-checks approval, 
    so nothing sent from here is trusted as content. 
    Both transport failures and refusals come back as an error string, so the caller handles them one way.

    Args:
        action: Either "publish" or "delete".
        card_id: Identifier of the card to act on.

    Returns:
        Tuple of (payload_dict, None) on success, or (None, error_string) on failure.
    """
    if not COMMITTER_FUNCTION_NAME:
        return None, "committer not configured"

    # Send only the action and the id, the committer sources everything else itself
    try:
        resp = lambda_client.invoke(
            FunctionName=COMMITTER_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": action, "cardId": card_id}).encode("utf-8"),
        )
        # Distinguish a crashed invocation from a clean refusal
        payload = json.loads(resp["Payload"].read() or "{}")
        if resp.get("FunctionError"):
            return None, f"committer error: {payload}"
        if payload.get("error"):
            return None, payload["error"]
        return payload, None
    except Exception as e:
        return None, str(e)


def _invoke_publisher(card_id):
    """
    Publishes a card by asking the committer to commit it.

    Unwraps the committer's response down to the one value the caller needs.

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

    These never reached DynamoDB, because the gate quarantined them before any record existed, 
    so the bucket prefix is all there is to read. 
    Placeholders and debug overlays are skipped.

    Args:
        None. The bucket and prefix come from module-level configuration.

    Returns:
        Tuple of (items, None) on success, where each item is a dict with key, name, url and size;
        or (None, error_string) on failure.
    """
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    try:
        items = []
        token = None

        # Page through the whole prefix; a truncated listing would hide uploads
        while True:
            kw = {"Bucket": IMAGES_BUCKET, "Prefix": REJECTED_PREFIX}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                # Skip the prefix placeholder and debug overlays — only real photos
                k = o["Key"]
                if k.endswith("/") or k.endswith(".keep") or k.endswith("__corners.png"):
                    continue
                items.append({
                    "key": k,
                    "name": k.split("/")[-1],
                    "url": _rejected_preview_url(k),
                    "size": o.get("Size", 0),  # bytes (from list_objects_v2)
                })
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        items.sort(key=lambda i: i["name"])
        return items, None
    except Exception as e:
        return None, str(e)


def _rejected_preview_url(key):
    """
    Builds a short-lived pre-signed URL so a rejected upload can be previewed.

    The bucket is private, so the browser cannot fetch these directly. 
    A failure returns an empty string and the row simply renders without a thumbnail, 
    rather than breaking the whole Rejected tab.

    Args:
        key: S3 key of the rejected object.

    Returns:
        The pre-signed GET URL, or an empty string on failure.
    """
    if not IMAGES_BUCKET:
        return ""
    try:
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": IMAGES_BUCKET, "Key": key},
            ExpiresIn=REJECTED_URL_TTL,
        )
    except Exception:
        return ""


def _reprocess_rejected(key):
    """
    Rescues a wrongly rejected upload by putting it back into the processing path.

    This is the only place a human overrules a model. 
    The image goes back into the raw prefix with a force-process marker, so the gate forwards it 
    without scoring it again, and the quarantined copy is removed. 
    The key must be under the quarantine prefix.

    Args:
        key: S3 key of the rejected object, which must be under the quarantine prefix.

    Returns:
        Tuple of (raw_key, None) on success, or (None, error_string) on failure.
    """
    if not key or not key.startswith(REJECTED_PREFIX):
        return None, f"key must be under {REJECTED_PREFIX}"
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    raw_key = RAW_PREFIX + key[len(REJECTED_PREFIX):]
    try:
        # Read and write the bytes rather than copy_object
        obj = s3.get_object(Bucket=IMAGES_BUCKET, Key=key)
        body = obj["Body"].read()
        md = dict(obj.get("Metadata", {}))  # preserve uploader (+ anything else)
        md[FORCE_PROCESS_META] = "1"
        s3.put_object(
            Bucket=IMAGES_BUCKET,
            Key=raw_key,
            Body=body,
            Metadata=md,
            ContentType=obj.get("ContentType", "image/jpeg"),
        )
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
        return raw_key, None
    except Exception as e:
        return None, str(e)


def _delete_rejected(key):
    """
    Dismisses a rejected upload from the quarantine prefix.

    The bucket is versioned, so a mistaken dismissal is still recoverable and the key must be
    under the quarantine prefix, so this cannot reach cards or raw uploads.

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
    except Exception as e:
        return None, str(e)


# Admin's html page
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

    Every rendered field goes through here, because they all come from anonymous uploads or a model's draft text.

    Args:
        x: String to escape, or None.

    Returns:
        The HTML-escaped string, or an empty string if the input was None.
    """
    return html.escape(x or "")


def render_list(status_filter, saved_id=None, note=None, deleted_id=None):
    """
    Renders the main card list page.

    One row per card, with its status and publish state. 
    The optional arguments drive the banner, so the result of a save or delete survives the redirect that follows it.

    Args:
        status_filter: Status to filter by, or None for all cards.
        saved_id: Card just saved, highlighted with a confirmation banner.
        note: Free-text message to show in the banner.
        deleted_id: Card just deleted, reported in the banner.

    Returns:
        Complete HTML document as a string.
    """
    rows = []
    items = list_cards(status_filter)
    for it in items:
        cid = _s(it, "cardId")
        st = _s(it, "status")
        st_cls = "status-pending" if st == "pending" else "status-approved" if st == "approved" else ""

        # Shows whether an approved card actually reached GitHub
        ps = _s(it, "publishStatus")
        ps_cls = ("publish-published" if ps == "published"
                  else "publish-failed" if ps == "failed"
                  else "publish-pending" if ps == "pending" else "")
        title = _s(it, "title") or _s(it, "title_ai_draft") or "(untitled)"

        # A GET to the confirm page
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

            # One HeadObject per row, which is fine at this size
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
        
        # The delete flow reuses the note channel with a single del_err token
        err = ""
        for t in (note.split(";;") if note else []):
            if t.startswith("del_err:"):
                err = t[len("del_err:"):]
        if err:
            banner = (
                f"<div class='deleted'><b>Delete failed for {esc(deleted_id)}:</b> "
                f"{esc(err)} &middot; nothing was removed — try again.</div>"
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

    Shows the finished image next to every editable field, pre-filled from the model's drafts.
    Setting the status to approved is what arms publishing on the next save.

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
        
        # Prefill with the draft when nothing is approved yet
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

    # Shown once a publish has been attempted
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
        
        # Kill switch on the edit page
        f"<form class='del-form' method='get' action='{BASE}/delete' style='display:block; margin-top:10px;'>"
        f"<input type='hidden' name='cardId' value='{esc(card_id)}'>"
        f"<button class='del-btn del-btn-lg' type='submit'>Delete this card…</button>"
        f"</form>"
    )
    return f"<!doctype html><html><head><meta charset=utf-8><title>Edit {esc(card_id)}</title>{PAGE_CSS}</head><body>{body}</body></html>"


def render_delete_confirm(card_id):
    """CSP-safe confirmation page for the kill switch.
    Shows what will be removed and a CSRF-protected POST form that actually performs the delete. 
    Reached by the GET Delete buttons.

    Args:
        card_id: Identifier of the card queued for deletion.

    Returns:
        Complete HTML document as a string.
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

    # Shows exactly what the deleter will remove for specific card
    gh_line = (
        f"<li>GitHub: <code>{esc(slug)}.md</code> + <code>{esc(card_id)}.png</code> "
        f"on the live branch (the card is <b>published</b> — this takes it OFF the site)</li>"
        if slug and ps == "published"
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

    Both views render the same bar, so the tabs stay identical even though 
    one is backed by DynamoDB and the other by an S3 listing.

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
    Renders the Rejected tab: the quarantined photos, with no card details to show.

    Each one gets two buttons. "Put to process" sends it back through the pipeline, 
    where it reappears as a pending card. 
    "Delete" goes via a confirm page, like the card kill switch.

    Args:
        note: Optional message to show in the result banner.
        reprocessed: Key of the upload just sent back for processing, if any.
        deleted: Key of the upload just deleted, if any.

    Returns:
        Complete HTML document as a string.
    """
    items, err = _list_rejected()

    # Result banner
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
                size_mb = f"{it.get('size', 0) / 1048576:.2f} MB"  # bytes -> MB
                csrf = _csrf_token(key)  # CSRF id for a rejected object = its S3 key
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
    """Confirm page for deleting a rejected upload. 
    Shows the picture + a CSRF-protected POST.

    Args:
        key: S3 key of the rejected upload queued for deletion.

    Returns:
        Complete HTML document as a string.
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
            
            # Admin pages show pending data
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            
            # Keeps the admin URL and cardId out of the Referer on outbound links
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

    Redirecting after a write is what stops a browser refresh from re-submitting the form 
    and repeating the action - the post/redirect/get pattern. 
    The default 303 makes the follow-up request a GET regardless of the original method.

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

    API Gateway may deliver the body base64-encoded depending on the content type, so the flag has to be 
    honoured rather than assumed - reading it raw would corrupt form submissions.

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
    Fetches the Basic Auth credentials from Secrets Manager, cached for a bounded time.

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
            return _credentials  # transient refresh failure -> keep valid cached creds
        raise  # cold cache + fetch failed -> fail closed (caller returns 503)


def _unauthorized():
    """
    Builds the 401 response that triggers the browser's native login prompt.

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
    Checks a request's Basic Auth credentials, gating every route.

    Args:
        event: API Gateway HTTP API payload v2.0 event, read for its authorization header.

    Returns:
        None when the credentials are valid, otherwise the error response the caller should return
        immediately — a 401 challenge, or a 503 when no usable secret is configured.
    """
    try:
        creds = _get_credentials()
    except Exception as e:  # secret missing / malformed / no permission -> deny
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
    Builds a per-card CSRF token - a keyed hash of the card id, using the auth password as the key.

    Args:
        card_id: The card the token is bound to; also used for rejected-upload keys.

    Returns:
        The token as a hex-encoded HMAC-SHA256 digest.
    """
    key = _get_credentials()["password"].encode("utf-8")
    return hmac.new(key, card_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _csrf_ok(card_id, token):
    """
    Verifies a submitted CSRF token against the expected one for a card.

    Any failure, including being unable to compute the expected token, counts as a rejection.

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


# Input validation
SLUG_MAX_LEN = 100  # <slug>.md has to stay a valid filename
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _validate(values):
    """
    Returns an error string if the submitted values are invalid, else "".

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
        
    # Checked whenever the slug is non-empty
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


def handle_save(event):
    """
    Handles the edit form post: validates, saves, and publishes when the card is approved.

    This is the human approval gate, and the only route that can put a card on the public site.

    Args:
        event: API Gateway event carrying the URL-encoded form body.

    Returns:
        A redirect response back to the list on success, or an HTML error response on a rejected
        CSRF token, a missing cardId or a validation failure.
    """

    # Parse the form and keep blank values so cleared fields are saved as empty
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    card_id = form.get("cardId", [""])[0]
    if not card_id:
        return _html_resp("<p>Missing cardId</p>", 400)

    # Require the signed token the edit form embedded
    if not _csrf_ok(card_id, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the edit page and try again.</p>"
            f"<a href='{BASE}/edit?cardId={urllib.parse.quote(card_id)}'>back</a>",
            403,
        )

    values = {f: form.get(f, [""])[0] for f in EDITABLE}

    # Validate before touching the DB
    problem = _validate(values)
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
    The kill switch: asks the committer to remove the card from GitHub, S3 and DynamoDB.

    Args:
        event: API Gateway event carrying the form body with cardId and CSRF token.

    Returns:
        A redirect back to the list, or an HTML error response if the CSRF check failed.
    """
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    card_id = form.get("cardId", [""])[0]
    if not card_id:
        return _html_resp("<p>Missing cardId</p>", 400)

    # The token only exists on the confirm page
    if not _csrf_ok(card_id, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the delete page and try again.</p>"
            f"<a href='{BASE}/delete?cardId={urllib.parse.quote(card_id)}'>back</a>",
            403,
        )

    _summary, err = _invoke_deleter(card_id)
    loc = f"{BASE}?deleted={urllib.parse.quote(card_id)}"
    if err:
        loc += f"&note={urllib.parse.quote('del_err:' + err)}"
    return _redirect(loc)


def handle_reprocess(event):
    """
    Handles "put to process": sends a rejected upload back through the pipeline.

    Nothing is destroyed, so this needs no confirm page. 
    The image reappears as a pending card once the processor finishes.

    Args:
        event: API Gateway event carrying the form body with the S3 key and CSRF token.

    Returns:
        A redirect back to the Rejected tab, or an HTML error response if the CSRF check failed.
    """
    form = urllib.parse.parse_qs(_read_body(event), keep_blank_values=True)
    key = form.get("key", [""])[0]
    if not key:
        return _html_resp("<p>Missing key</p>", 400)
    
    # A rejected object has no cardId yet, so the token is bound to its S3 key instead
    if not _csrf_ok(key, form.get("csrf", [""])[0]):
        return _html_resp(
            f"<p>Invalid or missing CSRF token. Reload the rejected page and try again.</p>"
            f"<a href='{BASE}?status=rejected'>back</a>", 403,
        )
    _raw_key, err = _reprocess_rejected(key)
    loc = f"{BASE}?status=rejected"
    if err:
        loc += f"&note={urllib.parse.quote('reproc_err:' + err)}"
    else:
        loc += f"&reprocessed={urllib.parse.quote(key)}"
    return _redirect(loc)


def handle_reject_delete(event):
    """
    Deletes a rejected upload from the quarantine prefix.

    The bucket is versioned, so this leaves a recoverable version behind the delete marker.

    Args:
        event: API Gateway event carrying the form body with the S3 key and CSRF token.

    Returns:
        A redirect back to the Rejected tab, or an HTML error response if the CSRF check failed.
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
    Lambda entry point: authorises the request, then routes it to a view or an action.

    Authorisation runs before any routing, so no route can be reached unauthenticated. 
    GETs render pages, POSTs change state and each verifies its own CSRF token.

    Args:
        event: API Gateway HTTP API payload v2.0 event.
        context: Lambda context object, unused.

    Returns:
        An API Gateway response dict — an HTML page, a redirect, or an auth challenge.
    """

    # Gate everything, before any routing happens
    denied = _authorize(event)
    if denied is not None:
        return denied

    # Route on method and path - unknown paths fall through to the list view
    rc = event.get("requestContext", {}).get("http", {})
    method = rc.get("method", "GET")
    path = rc.get("path") or event.get("rawPath") or BASE
    qs = event.get("queryStringParameters") or {}

    try:
        if method == "GET" and path in (BASE, BASE + "/"):
            
            # The Rejected tab has no database to read, so it renders separately
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
        
        # Kill switch: the GET shows a confirm page, the POST does the deleting
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
