# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci

"""Publishes an approved card to the website, or removes a published one.

The review editor invokes this lambda synchronously with an action and a cardId, and nothing else: 
every value that reaches GitHub or S3 is read here from DynamoDB. 
Publishing re-checks that the card is approved, then commits its markdown page and image to the live branch in one commit, 
so a page can never appear without its picture. 
Deleting removes the card from GitHub first, then S3, then DynamoDB..

This function holds the GitHub token and has no API Gateway in front of it. 
The review editor is on the internet but holds no token and cannot publish on its own.

Functions:
    handler: Entry point; dispatches on the action in the payload.
    do_publish: Re-reads and re-verifies the card, then commits it.
    do_delete: Removes the card from GitHub, S3 and DynamoDB.

Environment (set by Terraform):
    GITHUB_PAT_SECRET_ARN           Secrets Manager ARN of the fine-grained PAT
    GITHUB_OWNER / GITHUB_REPO      the repository to commit to
    GITHUB_PUBLISH_BRANCH           branch to commit and delete on (the live branch)
    GITHUB_BASE_BRANCH              the live branch; same as the publish branch
    GITHUB_CONTENT_DIR              where the markdown pages live
    GITHUB_IMAGE_DIR                where the card images live
    IMAGES_BUCKET                   private bucket holding the processed images
    DDB_TABLE                       card table, the source of truth for both actions
    AWS_REGION                      provided automatically by the Lambda runtime
"""

import os
import re
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

import boto3

REGION = os.environ.get("AWS_REGION", "eu-central-1")

GITHUB_PAT_SECRET_ARN = os.environ.get("GITHUB_PAT_SECRET_ARN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

# Commit and delete target; it must already exist, because the API will not create it
GITHUB_PUBLISH_BRANCH = os.environ.get("GITHUB_PUBLISH_BRANCH", "master")

# The live branch, which is the publish branch itself in this single-branch setup
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
GITHUB_CONTENT_DIR = os.environ.get("GITHUB_CONTENT_DIR", "web/awsbuildercards/content/collectibles")
GITHUB_IMAGE_DIR = os.environ.get("GITHUB_IMAGE_DIR", "web/awsbuildercards/static/cards")
IMAGES_BUCKET = os.environ.get("IMAGES_BUCKET", "")
DDB_TABLE = os.environ.get("DDB_TABLE", "BuilderCards")

secretsmanager = boto3.client("secretsmanager", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)

_github_token = None  # cached GitHub PAT after first Secrets Manager fetch

# Card fields read from DynamoDB to build the front matter
CARD_FIELDS = ["title", "slug", "weight", "event", "year", "effect",
               "description", "category", "subcategory", "uploader"]


# The slug becomes a path in the commit
SLUG_MAX_LEN = 100
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _s(item, key):
    """
    Reads a string attribute out of a raw DynamoDB item.

    The low-level client wraps every value in a type descriptor, so a string lives at item[key]["S"]. 
    A missing item or attribute gives an empty string, not an error.

    Args:
        item: Raw DynamoDB item dict as returned by get_item(), or None.
        key: Attribute name to read.

    Returns:
        The attribute's string value, or an empty string if the item or attribute is missing.
    """
    return item.get(key, {}).get("S", "") if item else ""


def _slug_problem(slug):
    """
    Validates that a slug is safe to interpolate into a GitHub commit path.

    Bad slugs are rejected rather than cleaned up, because a cleaned slug would be committed under one name while 
    DynamoDB kept another, leaving a card that cannot be deleted.
    fullmatch() is required here, since "$" would also match before a trailing newline.

    Args:
        slug: Candidate slug string, as read from DynamoDB.

    Returns:
        Empty string if the slug is safe, otherwise a human-readable reason describing the problem, 
        suitable for logging and returning to the caller.
    """
    if not slug:
        return "slug is empty"
    if len(slug) > SLUG_MAX_LEN:
        return f"slug too long ({len(slug)} chars, max {SLUG_MAX_LEN})"
    if not SLUG_RE.fullmatch(slug):
        return ("slug is not a safe path component (expected lowercase a-z / 0-9 "
                "joined by single hyphens)")
    return ""


def _get_github_token():
    """
    Fetches the GitHub personal access token from Secrets Manager.

    Cached for the life of the container, so a warm invocation does not fetch it again.

    Args:
        None. The secret ARN comes from the GITHUB_PAT_SECRET_ARN environment variable.

    Returns:
        The PAT as a stripped string.
    """
    global _github_token

    # Warm container: reuse the token already fetched
    if _github_token is not None:
        return _github_token

    # Cold start: fetch once and cache for the life of the container
    resp = secretsmanager.get_secret_value(SecretId=GITHUB_PAT_SECRET_ARN)
    _github_token = resp["SecretString"].strip()
    return _github_token


def _gh(method, path, body=None):
    """
    Makes a single authenticated call to the GitHub REST API.

    Built on urllib, so the deployment package needs no dependencies. 
    HTTP errors come back as a status code instead of an exception, because a 404 is a normal answer here.

    Args:
        method: HTTP verb, e.g. "GET", "POST" or "PATCH".
        path: API path beginning with a slash, appended to https://api.github.com.
        body: Optional dict serialised as the JSON request body.

    Returns:
        Tuple of (status_code, parsed_json) where the second element is None for empty or unparseable responses.
    """

    # Build the request; only attach a body and content type when one was given
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"https://api.github.com{path}", method=method, data=data)
    req.add_header("Authorization", f"Bearer {_get_github_token()}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "buildercards-committer-lambda")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    # Return HTTP errors as values, not exceptions — callers branch on the status code
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            parsed = json.loads(payload) if payload else None
        except Exception:
            parsed = None
        return e.code, parsed


def _yaml_dq(value):
    """
    Renders a string as a double-quoted YAML scalar for the Hugo front matter.
    Quotes, tabs and newlines are escaped, so card text cannot break out of its scalar and corrupt the front matter.

    Args:
        value: Raw string to quote, or None.

    Returns:
        The value wrapped in double quotes with all special characters escaped.
    """

    # Backslashes first, so the escapes added below are not themselves escaped
    s = value or ""
    s = s.replace("\\", "\\\\").replace('"', '\\"')

    # Normalise line endings, then flatten newlines and tabs into escape sequences
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{s}"'


def build_front_matter(card_id, values):
    """
    Builds the Hugo front-matter block that becomes the card's markdown page.

    Weight and year stay unquoted so Hugo reads them as numbers, and the image path uses the cardId, 
    so renaming a card never breaks its picture. The page has no body content.

    Args:
        card_id: Stable card identifier, used to build the /cards/<cardId>.png image path.
        values: Dict of card fields read from DynamoDB (title, slug, weight, event, year, effect,
                description, category, subcategory, uploader).

    Returns:
        The complete front-matter block as a string, delimited by --- lines and newline-terminated.
    """

    # Uploader is a YAML list: one entry when present, empty list otherwise
    uploader_val = (values.get("uploader", "") or "").strip()
    uploader_line = f"uploader: [{_yaml_dq(uploader_val)}]" if uploader_val else "uploader: []"

    # Numeric fields stay unquoted so Hugo reads them as numbers, not strings
    year = (values.get("year", "") or "").strip()
    weight = (values.get("weight", "") or "").strip()

    # Assemble the block; every free-text field goes through the YAML quoter
    fm_lines = [
        "---",
        f"title: {_yaml_dq(values.get('title', ''))}",
        f"slug: {_yaml_dq(values.get('slug', ''))}",
        f"weight: {weight}",
        f"event: {_yaml_dq(values.get('event', ''))}",
        f"year: {year}",
        f"effect: {_yaml_dq(values.get('effect', ''))}",
        f'image: "/cards/{card_id}.png"',
        f"description: {_yaml_dq(values.get('description', ''))}",
        f'category: {_yaml_dq(values.get("category", "") or "collectibles")}',
        f'subcategory: {_yaml_dq(values.get("subcategory", ""))}',
        uploader_line,
        "---",
    ]
    return "\n".join(fm_lines) + "\n"


def _gh_commit_files(repo, branch, files, message):
    """
    Commits several files to a branch atomically, in a single commit.
    The Contents API can only write one file per commit.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Target branch name, which must already exist — this API will not create it.
        files: List of (path, content_b64) tuples; content must be base64-encoded.
        message: Commit message.

    Returns:
        Tuple of (commit_html_url, "") on success, or ("", error_string) on any failure.
    """

    # Find the branch tip and the tree it points at
    code, ref = _gh("GET", f"{repo}/git/ref/heads/{branch}")
    if code != 200 or not isinstance(ref, dict):
        return "", f"get branch ref failed (HTTP {code}) — does '{branch}' exist?"
    base_commit_sha = ref["object"]["sha"]

    code, base_commit = _gh("GET", f"{repo}/git/commits/{base_commit_sha}")
    if code != 200 or not isinstance(base_commit, dict):
        return "", f"get base commit failed (HTTP {code})"
    base_tree_sha = base_commit["tree"]["sha"]

    # Upload every file as a blob and collect the tree entries
    tree = []
    for path, content_b64 in files:
        code, blob = _gh("POST", f"{repo}/git/blobs", {"content": content_b64, "encoding": "base64"})
        if code != 201 or not isinstance(blob, dict):
            return "", f"create blob failed for {path} (HTTP {code})"
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # Build a new tree on top of the base tree, so untouched files are preserved
    code, new_tree = _gh("POST", f"{repo}/git/trees", {"base_tree": base_tree_sha, "tree": tree})
    if code != 201 or not isinstance(new_tree, dict):
        return "", f"create tree failed (HTTP {code})"

    # Create the commit object pointing at that tree
    code, new_commit = _gh("POST", f"{repo}/git/commits", {
        "message": message,
        "tree": new_tree["sha"],
        "parents": [base_commit_sha],
    })
    if code != 201 or not isinstance(new_commit, dict):
        return "", f"create commit failed (HTTP {code})"

    # Move the branch: the single step that makes everything above visible
    code, _ = _gh("PATCH", f"{repo}/git/refs/heads/{branch}", {"sha": new_commit["sha"], "force": False})
    if code != 200:
        return "", f"update branch ref failed (HTTP {code})"

    return new_commit.get("html_url", ""), ""


def publish_to_github(card_id, values, finished_key=""):
    """
    Commits a card's markdown page and its image to the publish branch in one commit.

    The image is required: if it cannot be read the publish fails, so no page ever points at a missing picture. 
    Changing a card's slug and re-publishing orphans the old markdown file, which has to be deleted by hand.

    Args:
        card_id: Stable card identifier, used for the image filename.
        values: Card fields read from DynamoDB, including the slug.
        finished_key: S3 key of the processed PNG; publishing fails if empty.

    Returns:
        Tuple of (published_url, None) on success, or (None, error_string) on any failure.
    """

    # Nothing to do without GitHub configuration
    if not (GITHUB_PAT_SECRET_ARN and GITHUB_OWNER and GITHUB_REPO):
        return None, "GitHub not configured"

    # Validate the slug BEFORE it is interpolated into a commit path
    slug = (values.get("slug", "") or "").strip()
    problem = _slug_problem(slug)
    if problem:
        print(f"[committer] refusing to publish {card_id}: {problem}: {slug!r}")
        return None, problem

    repo = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    branch = GITHUB_PUBLISH_BRANCH

    # Build the markdown page from the card's fields
    md_b64 = base64.b64encode(build_front_matter(card_id, values).encode("utf-8")).decode()

    # The image is mandatory: never publish a page whose picture is missing
    if not finished_key:
        return None, "card has no finishedKey (no image to publish)"
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    try:
        obj = s3.get_object(Bucket=IMAGES_BUCKET, Key=finished_key)
        img_b64 = base64.b64encode(obj["Body"].read()).decode()
    except Exception as ex:  # noqa: BLE001
        return None, f"read finished image failed: {ex}"

    # Markdown named by slug, image named by cardId — both in one atomic commit
    files = [
        (f"{GITHUB_CONTENT_DIR}/{slug}.md", md_b64),
        (f"{GITHUB_IMAGE_DIR}/{card_id}.png", img_b64),
    ]
    _commit_url, err = _gh_commit_files(repo, branch, files, f"card: {slug} ({card_id})")
    if err:
        return None, f"commit failed: {err}"

    published_url = (
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/"
        f"{branch}/{GITHUB_CONTENT_DIR}/{slug}.md"
    )
    return published_url, None


def do_publish(card_id):
    """
    Publish action: re-reads the card from DynamoDB, re-verifies approval, then commits it.

    Approval is checked here rather than trusted from the request, so a caller cannot publish a card nobody approved. 
    That check is what lets the review editor sit on the internet.

    Args:
        card_id: Identifier of the card to publish.

    Returns:
        Dict with "published_url" on success, or "error" describing why it was refused or failed.
    """

    # Read the card; the caller's own content is never used
    resp = ddb.get_item(TableName=DDB_TABLE, Key={"cardId": {"S": card_id}})
    item = resp.get("Item")
    if not item:
        return {"error": f"card {card_id} not found"}

    # Re-verify approval here — the human gate is enforced at this sink
    status = _s(item, "status")
    if status != "approved":
        return {"error": f"card {card_id} not approved (status={status!r})"}

    # Collect the publishable fields and the image key
    values = {f: _s(item, f) for f in CARD_FIELDS}
    finished_key = _s(item, "finishedKey")

    url, err = publish_to_github(card_id, values, finished_key=finished_key)
    if err:
        print(f"[committer] publish failed for {card_id}: {err}")
        return {"error": err}
    print(f"[committer] published {card_id} -> {url}")
    return {"published_url": url or ""}


# DELETE

def _corners_key(raw_key):
    """
    Derives the key of the debug corners-overlay image the image-processor wrote.
    Mirrors the processor's naming, so the overlay is deleted with the card instead of being left behind in the bucket.

    Args:
        raw_key: S3 key of the original upload, or an empty string.

    Returns:
        The corresponding corners-overlay key, or an empty string if raw_key was empty.
    """
    if not raw_key:
        return ""

    # Strip the extension, if any, and append the overlay suffix
    base = raw_key.rsplit(".", 1)[0] if "." in raw_key else raw_key
    return f"{base}__corners.png"


def _delete_s3(key):
    """
    Deletes a single S3 object, best effort.
    The bucket is versioned, so the object stays recoverable, and deleting a missing key is a no-op. 
    Errors are returned instead of raised, so one bad object is a warning, not a failure.

    Args:
        key: S3 key to delete; an empty key is treated as nothing to do.

    Returns:
        Empty string on success or when there was nothing to delete, otherwise an error string prefixed with the key.
    """
    if not key:
        return ""
    if not IMAGES_BUCKET:
        return "IMAGES_BUCKET not configured"

    # Versioned bucket: this writes a delete marker, so the object stays recoverable
    try:
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
        return ""
    except Exception as ex:
        return f"{key}: {ex}"


def _gh_exists(repo, branch, path):
    """
    Checks whether a file currently exists on a branch.
    Called before a delete commit is built, because asking Git to remove a path that is not there fails the whole commit.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Branch to look on.
        path: Repository-relative file path.

    Returns:
        True if the file exists on that branch, False otherwise.
    """
    code, _ = _gh("GET", f"{repo}/contents/{urllib.parse.quote(path)}?ref={branch}")
    return code == 200


def _gh_delete_files(repo, branch, paths, message):
    """
    Removes several files from a branch atomically, in a single commit.

    The mirror of _gh_commit_files(), except the tree entries carry a null sha, which is how the API expresses a deletion. 
    A card that was never published produces no commit at all.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Branch to delete from.
        paths: Repository-relative paths to remove; non-existent ones are skipped.
        message: Commit message.

    Returns:
        Tuple of (commit_html_url, "") on success, ("", "") when there was nothing to delete, or
        ("", error_string) on failure.
    """

    # Only delete what is actually there; a never-published card has nothing
    existing = [p for p in paths if _gh_exists(repo, branch, p)]
    if not existing:
        return "", ""  # nothing published to remove

    # Find the branch tip and the tree it points at
    code, ref = _gh("GET", f"{repo}/git/ref/heads/{branch}")
    if code != 200 or not isinstance(ref, dict):
        return "", f"get branch ref failed (HTTP {code}) — does '{branch}' exist?"
    base_commit_sha = ref["object"]["sha"]

    code, base_commit = _gh("GET", f"{repo}/git/commits/{base_commit_sha}")
    if code != 200 or not isinstance(base_commit, dict):
        return "", f"get base commit failed (HTTP {code})"
    base_tree_sha = base_commit["tree"]["sha"]

    # A null sha on a tree entry is how the Git Data API expresses "delete this path"
    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in existing]
    code, new_tree = _gh("POST", f"{repo}/git/trees", {"base_tree": base_tree_sha, "tree": tree})
    if code != 201 or not isinstance(new_tree, dict):
        return "", f"create tree failed (HTTP {code})"

    # Create the commit object pointing at the reduced tree
    code, new_commit = _gh("POST", f"{repo}/git/commits", {
        "message": message,
        "tree": new_tree["sha"],
        "parents": [base_commit_sha],
    })
    if code != 201 or not isinstance(new_commit, dict):
        return "", f"create commit failed (HTTP {code})"

    # Move the branch: the single step that takes the card off the live site
    code, _ = _gh("PATCH", f"{repo}/git/refs/heads/{branch}", {"sha": new_commit["sha"], "force": False})
    if code != 200:
        return "", f"update branch ref failed (HTTP {code})"

    return new_commit.get("html_url", ""), ""


def do_delete(card_id):
    """
    Delete action: removes a card from GitHub, then S3, then DynamoDB.

    Args:
        card_id: Identifier of the card to remove.

    Returns:
        Dict with "deleted", "removed" (what was actually removed), "github" (commit URL), plus
        optional "github_skipped" and "s3_warnings" keys; or "error" if the deletion was aborted.
    """

    # Read the card to find everything that belongs to it
    resp = ddb.get_item(TableName=DDB_TABLE, Key={"cardId": {"S": card_id}})
    item = resp.get("Item")
    if not item:
        return {"error": f"card {card_id} not found"}

    slug = _s(item, "slug")
    raw_key = _s(item, "rawKey")
    finished = _s(item, "finishedKey") or f"images/finished/{card_id}.png"
    corners = _corners_key(raw_key)

    removed = []
    github_url = ""

    # An unsafe slug skips the GitHub step but still deletes the rest, so the kill switch
    slug_problem = _slug_problem(slug) if slug else ""
    if slug_problem:
        print(f"[committer] SKIPPING github delete for {card_id}: {slug_problem}: "
              f"{slug!r} — check for a stale .md by hand")

    # 1. GitHub first, to take the card off the site; only published cards have a slug
    if slug and not slug_problem and GITHUB_PAT_SECRET_ARN and GITHUB_OWNER and GITHUB_REPO:
        repo = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        paths = [f"{GITHUB_CONTENT_DIR}/{slug}.md", f"{GITHUB_IMAGE_DIR}/{card_id}.png"]
        github_url, gh_err = _gh_delete_files(
            repo, GITHUB_PUBLISH_BRANCH, paths, f"delete card: {slug} ({card_id})"
        )
        if gh_err:
            print(f"[committer] github delete failed for {card_id}: {gh_err}")
            return {"error": f"github delete failed: {gh_err}"}
        if github_url:
            removed.append("github:md+png")

    # 2. S3 objects (soft deletes; missing = ignored). Warnings, not fatal
    s3_errors = []
    for label, key in (("finished", finished), ("raw", raw_key), ("corners", corners)):
        err = _delete_s3(key)
        if err:
            s3_errors.append(err)
        elif key:
            removed.append(f"s3:{label}")

    # 3. DynamoDB item LAST (guarded)
    try:
        ddb.delete_item(
            TableName=DDB_TABLE,
            Key={"cardId": {"S": card_id}},
            ConditionExpression="attribute_exists(cardId)",
        )
        removed.append("dynamodb")
    except ddb.exceptions.ConditionalCheckFailedException:
        return {"error": f"card {card_id} vanished before delete (already gone?)"}
    except Exception as ex:  # noqa: BLE001
        print(f"[committer] dynamodb delete failed for {card_id}: {ex}")
        return {"error": f"dynamodb delete failed: {ex}"}

    out = {"deleted": card_id, "removed": removed, "github": github_url}
    if slug_problem:
        out["github_skipped"] = slug_problem
    if s3_errors:
        out["s3_warnings"] = s3_errors
    print(f"[committer] deleted {card_id}: {out}")
    return out


def handler(event, context):
    """
    Lambda entry point: dispatches to the publish or delete action.

    Only the action and the cardId are taken from the caller; everything else is read from DynamoDB. 
    An unknown action is refused rather than defaulted to anything.

    Args:
        event: Invocation payload with "action" ("publish" or "delete") and "cardId".
        context: Lambda context object, unused.

    Returns:
        Whatever the dispatched action returns, or a dict with "error" for a missing cardId or an
        unknown action.
    """

    # Only the action and the id are taken from the caller
    action = (event or {}).get("action", "")
    card_id = (event or {}).get("cardId", "")
    if not card_id:
        return {"error": "missing cardId"}

    # Dispatch; anything unrecognised is refused rather than guessed at
    if action == "publish":
        return do_publish(card_id)
    if action == "delete":
        return do_delete(card_id)
    return {"error": f"unknown action: {action!r} (expected 'publish' or 'delete')"}
