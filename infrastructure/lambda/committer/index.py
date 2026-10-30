# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michal Salanci
"""
Committer Lambda (BuilderCardsCommitter): publishes an approved card to GitHub, or deletes a card
everywhere.

It is the last step of the card pipeline and the only component that holds the GitHub token and the
power to delete cards. The review editor invokes it synchronously (RequestResponse); it has no API
Gateway and no other trigger. The editor holds only lambda:InvokeFunction on this function, so the
internet-facing editor never sees the token and cannot delete anything itself. The payload is
{"action": "publish" | "delete", "cardId": "..."}; every other value comes from the DynamoDB card
table, the single source of truth.

- publish: re-checks that the card is approved, then commits <slug>.md (the Hugo front matter) and
  <cardId>.png (the finished image from S3) in one commit, straight to the publish branch, which in
  the single-branch setup is the branch Amplify builds. Returns {"published_url": "..."} or
  {"error": "..."}.
- delete, the per-card kill switch: removes the card's files from GitHub, then its S3 objects, then
  its DynamoDB item, in that order so a failure part-way can be retried. Returns "deleted",
  "removed" and "github", plus "github_skipped" and "s3_warnings" when they apply, or
  {"error": "..."}.

Environment variables:
    GITHUB_PAT_SECRET_ARN: Secrets Manager ARN of the fine-grained GitHub token.
    GITHUB_OWNER, GITHUB_REPO: The repository the site is built from.
    GITHUB_PUBLISH_BRANCH: The branch both actions commit to; in the single-branch setup it is the
        live branch that Amplify builds (master). It must already exist, because the Git Data API
        does not create branches. Terraform always sets it; the default is only a fallback.
    GITHUB_CONTENT_DIR, GITHUB_IMAGE_DIR: The repository folders, from its root, for the markdown
        pages and the card images. Terraform always sets them; the defaults, which fit a Hugo site at
        the root of its repository, are only a fallback.
    IMAGES_BUCKET: The private image bucket; publish reads the finished PNG from it, delete removes
        the card's objects.
    DDB_TABLE: The card table, read by both actions.
    AWS_REGION: Set by the Lambda runtime.

CARD_FIELDS are the card attributes copied into the front matter: the fields the review editor
edits, except status. A new field reaches the site only when it is added both here and to
build_front_matter().

SLUG_MAX_LEN and SLUG_RE describe the only slugs this Lambda puts into a commit path: the shape
the image-processor's slugify() produces (lowercase a-z and 0-9 groups joined by single hyphens),
at most 100 characters long. They match the review editor's check; _slug_problem() says why the
check is repeated here.

CARD_ID_RE is the shape of every cardId that get-upload-url makes: a lowercase UUID. The cardId
becomes the image's file name in the commit, so it is checked like the slug.

AWS_CONFIG bounds every S3, DynamoDB and Secrets Manager call: 2 seconds to connect, 5 seconds per
read, at most 2 attempts. A hanging AWS call then ends within about 14 seconds instead of using up the
function's 30-second timeout, while the review editor waits for the answer.

An error that an action does not handle itself is answered with its type only. The full message goes
to the log, with the GitHub token removed, because an exception message can quote the token.
"""

import os
import re
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "eu-central-1")

GITHUB_PAT_SECRET_ARN = os.environ.get("GITHUB_PAT_SECRET_ARN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_PUBLISH_BRANCH = os.environ.get("GITHUB_PUBLISH_BRANCH", "master")
GITHUB_CONTENT_DIR = os.environ.get("GITHUB_CONTENT_DIR", "content/collectibles")
GITHUB_IMAGE_DIR = os.environ.get("GITHUB_IMAGE_DIR", "static/cards")
IMAGES_BUCKET = os.environ.get("IMAGES_BUCKET", "")
DDB_TABLE = os.environ.get("DDB_TABLE", "BuilderCards")

AWS_CONFIG = Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2, "mode": "standard"})

secretsmanager = boto3.client("secretsmanager", region_name=REGION, config=AWS_CONFIG)
s3 = boto3.client("s3", region_name=REGION, config=AWS_CONFIG)
ddb = boto3.client("dynamodb", region_name=REGION, config=AWS_CONFIG)

_github_token = None

CARD_FIELDS = ["title", "slug", "weight", "event", "year", "effect",
               "description", "category", "subcategory", "uploader"]

SLUG_MAX_LEN = 100
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CARD_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _s(item, key):
    """
    Reads a string attribute out of a raw DynamoDB item.

    The low-level DynamoDB client returns every attribute wrapped in a type descriptor, so a string
    lives at item[key]["S"]. This unwraps that safely and returns an empty string when the item is
    None or the attribute is absent, so callers never have to guard each field individually.

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

    The slug becomes a path component in both publish_to_github() and do_delete(), and the Git Data
    API takes that path raw, so an unchecked value such as "../../x" can commit a file anywhere in
    the repository, on the branch Amplify builds, which amounts to code execution at build time.
    urllib.parse.quote() in _gh_exists() is no protection: its default safe="/" and the unreserved
    "." let such a path through unchanged.
    The review editor runs the same check, but it is repeated here, at the sink: this function holds
    the GitHub token, trusts neither its caller nor DynamoDB, and a stored row can hold a slug that
    never passed the editor's check.
    The check requires a non-empty string within the length limit whose shape matches exactly
    what the image-processor's slugify() produces: lowercase alphanumeric groups joined by single
    hyphens.
    This uses fullmatch() rather than match() with an anchored pattern, because "$" also matches
    immediately BEFORE a trailing newline — re.match(r"^[a-z0-9-]+$", "aws-card\\n") would pass.
    Bad slugs are REJECTED rather than sanitized on purpose: sanitizing would commit
    <sanitized>.md while DynamoDB kept the raw value, so do_delete would later build a different
    path, _gh_exists would miss it, and a live card would become un-deletable after its database
    row had already been removed.

    Args:
        slug: Candidate slug string, as read from DynamoDB.

    Returns:
        Empty string if the slug is safe, otherwise a human-readable reason describing the
        problem, suitable for logging and returning to the caller.
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
    Fetches the GitHub fine-grained personal access token from Secrets Manager.

    The token is cached in a module-level global after the first call, so a warm container reuses
    it across invocations instead of paying a Secrets Manager round trip per API call; a rotated
    token is therefore picked up only by a new container. This Lambda is the only component in the
    stack that holds the PAT — the internet-facing review editor never receives it and can only
    invoke this function.

    Args:
        None. The secret ARN comes from the GITHUB_PAT_SECRET_ARN environment variable.

    Returns:
        The PAT as a stripped string.

    Raises:
        botocore.exceptions.ClientError: The secret could not be read.
    """
    global _github_token

    if _github_token is not None:
        return _github_token

    resp = secretsmanager.get_secret_value(SecretId=GITHUB_PAT_SECRET_ARN)
    _github_token = resp["SecretString"].strip()
    return _github_token


def _redact(text):
    """
    Removes the GitHub token from a text before it is logged.

    http.client refuses a header value that contains a line break and quotes the whole value in its
    error, so a token stored with a line break inside would appear in the exception message. Each
    whitespace-separated part of the token is removed, because the message shows the break escaped.

    Args:
        text: The text to clean, typically an exception message.

    Returns:
        The text with every part of the cached token replaced by "[redacted]"; the text unchanged when
        no token has been read yet.
    """
    for part in (_github_token or "").split():
        text = text.replace(part, "[redacted]")
    return text


def _gh(method, path, body=None):
    """
    Makes a single authenticated call to the GitHub REST API.

    The request is built with urllib rather than a third-party HTTP client, so the deployment
    package stays dependency-free. Every call carries the token, the API version pin and a
    User-Agent header, with a 3-second timeout on each network operation: a publish or a delete
    makes up to seven calls, and the review editor waits for the answer synchronously behind API
    Gateway's 30-second limit, so seven stalled calls must still end in about 21 seconds. HTTP
    error responses are returned like any other result rather than raised, because callers branch
    on status codes — a 404 from an existence check is a normal answer, not a failure. handler()
    catches the exceptions below and answers with an error.

    Args:
        method: HTTP verb, e.g. "GET", "POST" or "PATCH".
        path: API path beginning with a slash, appended to https://api.github.com.
        body: Optional dict serialised as the JSON request body; None sends no body.

    Returns:
        Tuple of (status_code, parsed_json). The second element is None for an empty body, or for
        an error response whose body is not JSON.

    Raises:
        urllib.error.URLError, TimeoutError: GitHub could not be reached or did not answer in time.
        json.JSONDecodeError: A success response's body is not JSON.
        botocore.exceptions.ClientError: The token could not be read from Secrets Manager.
    """

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"https://api.github.com{path}", method=method, data=data)
    req.add_header("Authorization", f"Bearer {_get_github_token()}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "buildercards-committer-lambda")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
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

    Backslashes are escaped first, so the backslashes added for double quotes, line breaks and tabs
    are not escaped again. Line endings are normalised and, along with tabs, replaced by their
    escape sequences, so a multi-line card description cannot break out of its quoted scalar and
    corrupt the front-matter block. Every other control character (U+0000 to U+001F and U+007F to
    U+009F) becomes a \\uNNNN escape, because the YAML specification does not allow them raw in a
    scalar, and a strict YAML parser rejects the whole front matter over one. Every escape used is
    also valid JSON, so the result reads back as a JSON string.

    Args:
        value: Raw string to quote, or None.

    Returns:
        The value wrapped in double quotes, with backslashes, double quotes and control characters
        escaped; None or an empty string gives "".
    """

    s = value or ""
    s = s.replace("\\", "\\\\").replace('"', '\\"')

    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: f"\\u{ord(m.group()):04x}", s)
    return f'"{s}"'


def build_front_matter(card_id, values):
    """
    Builds the Hugo front-matter block that becomes the card's markdown page.

    Every text field is passed through _yaml_dq() so card text cannot break the YAML, while weight
    and year are emitted unquoted because Hugo needs them as numbers; publish_to_github() has
    already checked that each is empty or ASCII digits. An empty category
    becomes "collectibles". The uploader is rendered as a YAML list — one entry when a name was
    given, empty otherwise. The image path is keyed by the stable cardId rather than the slug, so
    renaming a card never breaks its picture. This produces ONLY the front matter; the card has no
    body content.

    Args:
        card_id: Stable card identifier, used to build the /cards/<cardId>.png image path.
        values: Dict of card fields read from DynamoDB (title, slug, weight, event, year, effect,
            description, category, subcategory, uploader).

    Returns:
        The complete front-matter block as a string, delimited by --- lines and newline-terminated.
    """

    uploader_val = (values.get("uploader", "") or "").strip()
    uploader_line = f"uploader: [{_yaml_dq(uploader_val)}]" if uploader_val else "uploader: []"

    year = (values.get("year", "") or "").strip()
    weight = (values.get("weight", "") or "").strip()

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

    This uses the Git Data API rather than the Contents API, because the Contents API can only
    write one file per commit — which would put the markdown and its image in two separate commits
    and leave a window where the site references a picture that does not exist yet. Each file is
    uploaded as a blob, and the branch reference moves only once, at the very end, so either every
    file lands together or nothing changes at all. The new tree is built on the branch's current
    tree (base_tree), so every file not listed stays as it is; without it the commit would delete
    the rest of the repository. The final update is a non-forced patch, so a concurrent push is
    rejected rather than overwritten.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Target branch name, which must already exist — this API will not create it.
        files: List of (path, content_b64) tuples; content must be base64-encoded.
        message: Commit message.

    Returns:
        Tuple of (commit_html_url, "") on success, where the URL is empty if GitHub sent none, or
        ("", error_string) when an API call answers with an unexpected status or body.
    """

    code, ref = _gh("GET", f"{repo}/git/ref/heads/{branch}")
    if code != 200 or not isinstance(ref, dict):
        return "", f"get branch ref failed (HTTP {code}) — does '{branch}' exist?"
    base_commit_sha = ref["object"]["sha"]

    code, base_commit = _gh("GET", f"{repo}/git/commits/{base_commit_sha}")
    if code != 200 or not isinstance(base_commit, dict):
        return "", f"get base commit failed (HTTP {code})"
    base_tree_sha = base_commit["tree"]["sha"]

    tree = []
    for path, content_b64 in files:
        code, blob = _gh("POST", f"{repo}/git/blobs", {"content": content_b64, "encoding": "base64"})
        if code != 201 or not isinstance(blob, dict):
            return "", f"create blob failed for {path} (HTTP {code})"
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    code, new_tree = _gh("POST", f"{repo}/git/trees", {"base_tree": base_tree_sha, "tree": tree})
    if code != 201 or not isinstance(new_tree, dict):
        return "", f"create tree failed (HTTP {code})"

    code, new_commit = _gh("POST", f"{repo}/git/commits", {
        "message": message,
        "tree": new_tree["sha"],
        "parents": [base_commit_sha],
    })
    if code != 201 or not isinstance(new_commit, dict):
        return "", f"create commit failed (HTTP {code})"

    code, _ = _gh("PATCH", f"{repo}/git/refs/heads/{branch}", {"sha": new_commit["sha"], "force": False})
    if code != 200:
        return "", f"update branch ref failed (HTTP {code})"

    return new_commit.get("html_url", ""), ""


def publish_to_github(card_id, values, finished_key=""):
    """
    Commits a card's markdown page and its image to the publish branch in one commit.

    The cardId and the slug are validated before they are used anywhere, because they are
    interpolated straight into the commit paths. Weight and year must be empty or ASCII digits,
    because they go into the front matter unquoted, where a line break or "---" would add keys or
    end the block; this is checked here too, not only in the review editor, for the same reason as
    the slug. The front matter is built and base64-encoded, the finished PNG is read from the
    private S3 bucket, and both are committed together atomically. The image is REQUIRED — if it
    cannot be read the whole publish fails, so the site never ends up with a markdown page pointing
    at a missing picture. The markdown is named by slug while the image is named by the stable
    cardId, which is why the front matter points at /cards/<cardId>.png.
    Note on slug changes: changing a card's slug and re-publishing creates a new <newslug>.md and
    orphans the old one, which has to be deleted by hand. The PNG is keyed by cardId, so it always
    overwrites in place.

    Args:
        card_id: Stable card identifier, used for the image filename.
        values: Card fields read from DynamoDB, including the slug.
        finished_key: S3 key of the processed PNG; publishing fails if empty.

    Returns:
        Tuple of (published_url, None) on success, where published_url is the page's GitHub file
        URL on the publish branch; or (None, error_string) when GitHub is not configured, the
        cardId is not a UUID, the slug is unsafe, weight or year is not ASCII digits, the image
        cannot be read or the commit fails.
    """

    if not (GITHUB_PAT_SECRET_ARN and GITHUB_OWNER and GITHUB_REPO):
        return None, "GitHub not configured"

    if not CARD_ID_RE.fullmatch(card_id):
        print(f"[committer] refusing to publish {card_id!r}: cardId is not a UUID")
        return None, "cardId is not a UUID"

    slug = (values.get("slug", "") or "").strip()
    problem = _slug_problem(slug)
    if problem:
        print(f"[committer] refusing to publish {card_id}: {problem}: {slug!r}")
        return None, problem

    for field in ("weight", "year"):
        value = (values.get(field, "") or "").strip()
        if not re.fullmatch(r"[0-9]*", value):
            print(f"[committer] refusing to publish {card_id}: {field} is not ASCII digits: {value!r}")
            return None, f"{field} must be empty or ASCII digits"

    repo = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    branch = GITHUB_PUBLISH_BRANCH

    md_b64 = base64.b64encode(build_front_matter(card_id, values).encode("utf-8")).decode()

    if not finished_key:
        return None, "card has no finishedKey (no image to publish)"
    if not IMAGES_BUCKET:
        return None, "IMAGES_BUCKET not configured"
    try:
        obj = s3.get_object(Bucket=IMAGES_BUCKET, Key=finished_key)
        img_b64 = base64.b64encode(obj["Body"].read()).decode()
    except Exception as ex:  # noqa: BLE001
        return None, f"read finished image failed: {ex}"

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

    The caller only supplies a cardId — every value that ends up on the site is read here from
    DynamoDB, which is the single source of truth. The status is re-checked against "approved"
    rather than trusted from the request, so a caller cannot publish a card the human never
    approved. This re-verification is the reason the review editor can be internet-facing without
    holding any publishing power itself.

    Args:
        card_id: Identifier of the card to publish.

    Returns:
        Dict with "published_url" on success, or "error" describing why it was refused or failed.
        On success the card is committed to the publish branch. A failed publish and a success are
        logged; a missing or unapproved card is only reported in the returned dict.
    """

    resp = ddb.get_item(TableName=DDB_TABLE, Key={"cardId": {"S": card_id}})
    item = resp.get("Item")
    if not item:
        return {"error": f"card {card_id} not found"}

    status = _s(item, "status")
    if status != "approved":
        return {"error": f"card {card_id} not approved (status={status!r})"}

    values = {f: _s(item, f) for f in CARD_FIELDS}
    finished_key = _s(item, "finishedKey")

    url, err = publish_to_github(card_id, values, finished_key=finished_key)
    if err:
        print(f"[committer] publish failed for {card_id}: {err}")
        return {"error": err}
    print(f"[committer] published {card_id} -> {url}")
    return {"published_url": url or ""}


def _corners_key(raw_key):
    """
    Derives the key of the debug corners-overlay image that the image-processor may have written.

    The processor saves a diagnostic overlay next to a raw upload when it finds the card and its
    outline, named by stripping the original extension and appending a fixed suffix. This mirrors
    that naming exactly so the overlay is cleaned up along with the card, instead of being orphaned
    in the bucket.

    Args:
        raw_key: S3 key of the original upload, or an empty string.

    Returns:
        The corresponding corners-overlay key, or an empty string if raw_key was empty.
    """
    if not raw_key:
        return ""

    base = raw_key.rsplit(".", 1)[0] if "." in raw_key else raw_key
    return f"{base}__corners.png"


def _delete_s3(key):
    """
    Deletes a single S3 object, best effort.

    The bucket is versioned, so this writes a delete marker rather than destroying data — the
    object stays recoverable. Deleting a key that does not exist is a successful no-op, because
    DeleteObject is idempotent, which means a retried delete never fails on already-removed
    objects. Errors are returned as strings rather than raised, so one failed object becomes a
    warning instead of aborting the whole deletion.

    Args:
        key: S3 key to delete; an empty key is treated as nothing to do.

    Returns:
        Empty string on success or when there was nothing to delete; otherwise an error string,
        either the key followed by the error or "IMAGES_BUCKET not configured".
    """
    if not key:
        return ""
    if not IMAGES_BUCKET:
        return "IMAGES_BUCKET not configured"

    try:
        s3.delete_object(Bucket=IMAGES_BUCKET, Key=key)
        return ""
    except Exception as ex:  # noqa: BLE001
        return f"{key}: {ex}"


def _gh_exists(repo, branch, path):
    """
    Checks whether a file currently exists on a branch.

    Used before building a delete commit, so only files that are actually present are included —
    a pending card that was never published has none, and asking Git to remove a non-existent path
    would fail the whole commit. Only 200 means present and only 404 means absent. Any other answer
    (an expired or wrong token, a rate limit, an outage) is an error, because treating it as absent
    would let the delete go on to remove the card's images and record while it stays on the site.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Branch to look on.
        path: Repository-relative file path.

    Returns:
        Tuple of (exists, error): (True, "") for 200, (False, "") for 404, and (False, error_string)
        for any other status.
    """
    code, _ = _gh("GET", f"{repo}/contents/{urllib.parse.quote(path)}?ref={branch}")
    if code == 200:
        return True, ""
    if code == 404:
        return False, ""
    return False, f"existence check for {path} failed (HTTP {code})"


def _gh_delete_files(repo, branch, paths, message):
    """
    Removes several files from a branch atomically, in a single commit.

    The mirror image of _gh_commit_files(), without blobs: each tree entry carries a null sha,
    which is how the Git Data API expresses a deletion. Paths are filtered through _gh_exists()
    first, so a never-published card produces no commit at all rather than an error. As with
    publishing, the new tree is built on the current one and the branch reference moves only once
    at the end, so the markdown and its image disappear from the site together.

    Args:
        repo: Repository API path fragment, e.g. "/repos/<owner>/<repo>".
        branch: Branch to delete from.
        paths: Repository-relative paths to remove; non-existent ones are skipped.
        message: Commit message.

    Returns:
        Tuple of (commit_html_url, "") on success, ("", "") when _gh_exists() finds none of the
        paths, or ("", error_string) when an existence check or another API call answers with an
        unexpected status or body.
    """

    existing = []
    for p in paths:
        found, err = _gh_exists(repo, branch, p)
        if err:
            return "", err
        if found:
            existing.append(p)
    if not existing:
        return "", ""

    code, ref = _gh("GET", f"{repo}/git/ref/heads/{branch}")
    if code != 200 or not isinstance(ref, dict):
        return "", f"get branch ref failed (HTTP {code}) — does '{branch}' exist?"
    base_commit_sha = ref["object"]["sha"]

    code, base_commit = _gh("GET", f"{repo}/git/commits/{base_commit_sha}")
    if code != 200 or not isinstance(base_commit, dict):
        return "", f"get base commit failed (HTTP {code})"
    base_tree_sha = base_commit["tree"]["sha"]

    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in existing]
    code, new_tree = _gh("POST", f"{repo}/git/trees", {"base_tree": base_tree_sha, "tree": tree})
    if code != 201 or not isinstance(new_tree, dict):
        return "", f"create tree failed (HTTP {code})"

    code, new_commit = _gh("POST", f"{repo}/git/commits", {
        "message": message,
        "tree": new_tree["sha"],
        "parents": [base_commit_sha],
    })
    if code != 201 or not isinstance(new_commit, dict):
        return "", f"create commit failed (HTTP {code})"

    code, _ = _gh("PATCH", f"{repo}/git/refs/heads/{branch}", {"sha": new_commit["sha"], "force": False})
    if code != 200:
        return "", f"update branch ref failed (HTTP {code})"

    return new_commit.get("html_url", ""), ""


def do_delete(card_id):
    """
    Delete action: removes a card from GitHub, then S3, then DynamoDB.

    The order is deliberate and makes a partial failure retryable. GitHub goes first, because that
    is what takes a live card off the public site; if it fails the function aborts with the record
    still intact, so the same delete can simply be run again. S3 objects follow as soft deletes on
    the versioned bucket, and their failures are collected as warnings rather than treated as
    fatal. The DynamoDB item is removed LAST, guarded by attribute_exists, because it is the record
    that lets everything else be found — deleting it first would strand any remaining files.
    The GitHub step runs only for a card with a slug; a pending card with an empty slug has nothing
    on GitHub. For a card with a slug, missing GitHub configuration aborts the delete before
    anything is removed, as publish refuses too, because skipping GitHub would leave a live page
    with no record left to find it by. A card without a finishedKey falls back to
    images/finished/<cardId>.png.
    An unsafe slug, or a cardId that is not a UUID, is never put into a delete path either, because
    this is the same path-traversal sink as publish. Unlike publish, it skips only the GitHub step
    and still clears S3 and DynamoDB instead of aborting: publish_to_github() rejects the same
    values, so a validating committer cannot have committed such a card, while refusing here would
    make the kill switch unusable for exactly the records most worth removing. The skip is logged
    and returned as github_skipped, so a stale page committed without this check gets noticed and
    removed by hand.

    Args:
        card_id: Identifier of the card to remove.

    Returns:
        Dict with "deleted" (the cardId), "removed" (labels of the steps that succeeded), "github"
        (the delete commit URL, empty when nothing was removed from GitHub), plus "github_skipped"
        (why the GitHub step was skipped) and "s3_warnings" when they apply; or "error" if the
        deletion was aborted. A skipped GitHub step, a GitHub or DynamoDB failure, missing GitHub
        configuration and a success are logged; a missing or vanished card is only reported in the
        returned dict.
    """

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

    github_skipped = _slug_problem(slug) if slug else ""
    if slug and not github_skipped and not CARD_ID_RE.fullmatch(card_id):
        github_skipped = "cardId is not a UUID"
    if github_skipped:
        print(f"[committer] SKIPPING github delete for {card_id!r}: {github_skipped}: "
              f"slug {slug!r} — check for a stale .md and .png by hand")

    if slug and not github_skipped:
        if not (GITHUB_PAT_SECRET_ARN and GITHUB_OWNER and GITHUB_REPO):
            print(f"[committer] refusing to delete {card_id}: GitHub not configured")
            return {"error": "GitHub not configured; nothing was deleted"}
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

    s3_errors = []
    for label, key in (("finished", finished), ("raw", raw_key), ("corners", corners)):
        err = _delete_s3(key)
        if err:
            s3_errors.append(err)
        elif key:
            removed.append(f"s3:{label}")

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
    if github_skipped:
        out["github_skipped"] = github_skipped
    if s3_errors:
        out["s3_warnings"] = s3_errors
    print(f"[committer] deleted {card_id}: {out}")
    return out


def handler(event, context):
    """
    Lambda entry point: dispatches to the publish or delete action.

    This function has no API Gateway in front of it — it is invoke-only, called synchronously by
    the review editor, which holds nothing but permission to invoke it. That is what keeps the
    GitHub token out of the internet-facing component. The payload carries only an action and a
    cardId; the cardId must name an existing item, and every other value that reaches GitHub or S3
    is read from DynamoDB by the action itself, so caller-supplied content is never trusted. An
    unrecognised action is refused rather than defaulted to anything. Any error the actions do not
    catch themselves (GitHub unreachable or too slow, the token or the card unreadable) is logged
    and answered as {"error": ...}, so the review editor always gets an answer it can show. Nothing
    half-done reaches the site: the branch moves only at the end of a commit, and a delete removes
    S3 objects and the record only after GitHub.

    Args:
        event: Invocation payload with "action" ("publish" or "delete") and "cardId".
        context: Lambda context object, unused.

    Returns:
        Whatever the dispatched action returns, or a dict with "error" for a missing cardId, an
        unknown action or an error the action did not catch. For an uncaught error the answer names
        only the exception type ("publish failed: TimeoutError"); the message is logged, without the
        token.
    """

    action = (event or {}).get("action", "")
    card_id = (event or {}).get("cardId", "")
    if not card_id:
        return {"error": "missing cardId"}

    try:
        if action == "publish":
            return do_publish(card_id)
        if action == "delete":
            return do_delete(card_id)
    except Exception as ex:  # noqa: BLE001
        print(f"[committer] {action} failed for {card_id}: {type(ex).__name__}: {_redact(str(ex))}")
        return {"error": f"{action} failed: {type(ex).__name__}"}
    return {"error": f"unknown action: {action!r} (expected 'publish' or 'delete')"}
