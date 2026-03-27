#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


API_ACCEPT = "application/vnd.github+json"
USER_AGENT = "sing-box-forwarder/1.0"


@dataclass(frozen=True)
class Target:
    label: str
    artifact_name: str
    patterns: tuple[str, ...]


TARGETS = (
    Target(
        label="Windows amd64",
        artifact_name="binary-windows_amd64",
        patterns=(r"^sing-box-.*-windows-amd64\.zip$",),
    ),
    Target(
        label="Android arm64",
        artifact_name="binary-android_arm64",
        patterns=(r"^sing-box-.*-android-arm64\.tar\.gz$",),
    ),
    Target(
        label="OpenWrt x86_64",
        artifact_name="binary-linux_amd64-musl",
        patterns=(
            r"^sing-box_.*_openwrt_x86_64\.ipk$",
            r"^sing-box_.*_openwrt_x86_64\.apk$",
        ),
    ),
    Target(
        label="OpenWrt aarch64_generic",
        artifact_name="binary-linux_arm64-musl",
        patterns=(
            r"^sing-box_.*_openwrt_aarch64_generic\.ipk$",
            r"^sing-box_.*_openwrt_aarch64_generic\.apk$",
        ),
    ),
)


def log(message: str) -> None:
    print(message, flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def github_api_headers() -> dict[str, str]:
    headers = {
        "Accept": API_ACCEPT,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("SOURCE_ARTIFACT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_download_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    token = os.getenv("SOURCE_ARTIFACT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = API_ACCEPT
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


def retry_attempts() -> int:
    return max(1, int(os.getenv("HTTP_RETRY_ATTEMPTS", "3")))


def retry_delay_seconds() -> float:
    return max(0.0, float(os.getenv("HTTP_RETRY_DELAY_SECONDS", "5")))


def request_timeout_seconds() -> int:
    return max(30, int(os.getenv("HTTP_TIMEOUT_SECONDS", "180")))


def should_retry(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (urllib.error.URLError, TimeoutError))


def urlopen_with_retry(request: urllib.request.Request):
    last_error: Exception | None = None
    for attempt in range(1, retry_attempts() + 1):
        try:
            return urllib.request.urlopen(request, timeout=request_timeout_seconds())
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt >= retry_attempts() or not should_retry(error):
                raise
            log(
                f"Request failed on attempt {attempt}/{retry_attempts()}: {error}. "
                f"Retrying in {retry_delay_seconds()}s."
            )
            time.sleep(retry_delay_seconds())
    assert last_error is not None
    raise last_error


def http_get_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urlopen_with_retry(request) as response:
        return json.load(response)


def download_file(url: str, headers: dict[str, str], destination: Path) -> None:
    request = urllib.request.Request(url, headers=headers)
    with urlopen_with_retry(request) as response, destination.open("wb") as output:
        output.write(response.read())


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_processed_run_id": 0, "history": []}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def monitored_branches() -> set[str]:
    value = os.getenv("MONITORED_BRANCHES", "stable,unstable")
    return {branch.strip() for branch in value.split(",") if branch.strip()}


def list_pending_runs(
    owner: str,
    repo: str,
    workflow_file: str,
    last_processed_run_id: int,
) -> list[dict]:
    branches = monitored_branches()
    pending: list[dict] = []
    page = 1
    per_page = 100

    while True:
        params = urllib.parse.urlencode(
            {
                "status": "completed",
                "event": "push",
                "per_page": per_page,
                "page": page,
            }
        )
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
            f"{workflow_file}/runs?{params}"
        )
        payload = http_get_json(url, github_api_headers())
        runs = payload.get("workflow_runs", [])
        if not runs:
            break

        reached_old_runs = False
        for run in runs:
            run_id = int(run["id"])
            if run_id <= last_processed_run_id:
                reached_old_runs = True
                break
            if run.get("conclusion") != "success":
                continue
            if branches and run.get("head_branch") not in branches:
                continue
            pending.append(run)

        if reached_old_runs or len(runs) < per_page:
            break
        page += 1

    pending.sort(key=lambda run: int(run["id"]))
    return pending


def bootstrap_pending_runs(last_processed_run_id: int, pending_runs: list[dict]) -> list[dict]:
    if last_processed_run_id != 0 or not pending_runs:
        return pending_runs

    mode = os.getenv("BOOTSTRAP_MODE", "latest-only").strip().lower()
    if mode == "all":
        return pending_runs
    return [pending_runs[-1]]


def limit_pending_runs(pending_runs: list[dict]) -> list[dict]:
    raw_limit = os.getenv("MAX_RUNS", "").strip()
    if not raw_limit:
        return pending_runs

    limit = int(raw_limit)
    if limit <= 0:
        return pending_runs
    return pending_runs[:limit]


def list_artifacts(artifacts_url: str) -> list[dict]:
    payload = http_get_json(artifacts_url, github_api_headers())
    return payload.get("artifacts", [])


def artifact_download_url(owner: str, repo: str, run_id: int, artifact: dict) -> str:
    mode = os.getenv("ARTIFACT_DOWNLOAD_MODE", "nightly-link").strip().lower()
    if mode == "github-api" or os.getenv("SOURCE_ARTIFACT_TOKEN", "").strip():
        return artifact["archive_download_url"]

    base = os.getenv("NIGHTLY_LINK_BASE", "https://nightly.link").rstrip("/")
    artifact_name = urllib.parse.quote(artifact["name"], safe="")
    return f"{base}/{owner}/{repo}/actions/runs/{run_id}/{artifact_name}.zip"


def extract_zip(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(destination)


def find_matching_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    regexes = [re.compile(pattern) for pattern in patterns]
    matches: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(regex.match(path.name) for regex in regexes):
            matches.append(path)
    matches.sort(key=lambda path: path.name)
    return matches


def encode_multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            (
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    return b"".join(parts), boundary


def send_document(bot_token: str, chat_id: str, file_path: Path, caption: str) -> None:
    body, boundary = encode_multipart(
        {"chat_id": chat_id, "caption": caption},
        "document",
        file_path,
    )
    request = urllib.request.Request(
        url=f"https://api.telegram.org/bot{bot_token}/sendDocument",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urlopen_with_retry(request) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected {file_path.name}: {payload}")


def build_caption(run: dict, file_name: str, label: str) -> str:
    sha = run.get("head_sha", "")[:7]
    lines = [
        "sing-box new build",
        f"branch: {run.get('head_branch', 'unknown')}",
        f"commit: {sha}",
        f"target: {label}",
        f"file: {file_name}",
        f"run: {run.get('html_url', '')}",
    ]
    return "\n".join(lines)


def process_run(
    owner: str,
    repo: str,
    run: dict,
    bot_token: str,
    chat_id: str,
    dry_run: bool,
) -> list[str]:
    run_id = int(run["id"])
    artifacts = {artifact["name"]: artifact for artifact in list_artifacts(run["artifacts_url"])}
    sent_files: list[str] = []

    with tempfile.TemporaryDirectory(prefix=f"sing-box-run-{run_id}-") as temp_dir:
        temp_root = Path(temp_dir)

        for target in TARGETS:
            artifact = artifacts.get(target.artifact_name)
            if not artifact:
                raise RuntimeError(
                    f"Run {run_id} does not contain required artifact {target.artifact_name}."
                )
            if artifact.get("expired"):
                raise RuntimeError(
                    f"Artifact {target.artifact_name} from run {run_id} has expired."
                )

            zip_path = temp_root / f"{artifact['name']}.zip"
            extract_dir = temp_root / artifact["name"]
            download_url = artifact_download_url(owner, repo, run_id, artifact)

            log(f"Downloading {artifact['name']} from {download_url}")
            download_file(download_url, github_download_headers(), zip_path)
            extract_dir.mkdir(parents=True, exist_ok=True)
            extract_zip(zip_path, extract_dir)

            matches = find_matching_files(extract_dir, target.patterns)
            if not matches:
                raise RuntimeError(
                    f"No files matched {target.patterns!r} inside {artifact['name']} for run {run_id}."
                )

            for match in matches:
                log(f"Selected {match.name} for {target.label}")
                if dry_run:
                    sent_files.append(match.name)
                    continue
                caption = build_caption(run, match.name, target.label)
                send_document(bot_token, chat_id, match, caption)
                sent_files.append(match.name)
                log(f"Sent {match.name} to Telegram")

    return sent_files


def main() -> int:
    owner = os.getenv("SOURCE_OWNER", "yelnoo").strip()
    repo = os.getenv("SOURCE_REPO", "sing-box").strip()
    workflow_file = os.getenv("SOURCE_WORKFLOW_FILE", "build.yml").strip()
    state_path = Path(os.getenv("STATE_FILE", "state/state.json"))
    dry_run = env_bool("DRY_RUN", default=False)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not dry_run:
        bot_token = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = require_env("TELEGRAM_CHAT_ID")

    state = load_state(state_path)
    last_processed_run_id = int(state.get("last_processed_run_id", 0))
    log(f"Last processed run id: {last_processed_run_id}")

    pending_runs = list_pending_runs(owner, repo, workflow_file, last_processed_run_id)
    pending_runs = bootstrap_pending_runs(last_processed_run_id, pending_runs)
    pending_runs = limit_pending_runs(pending_runs)
    if not pending_runs:
        log("No new successful upstream runs to forward.")
        return 0

    history = list(state.get("history", []))

    for run in pending_runs:
        run_id = int(run["id"])
        log(f"Processing run {run_id} on branch {run.get('head_branch')}")
        try:
            sent_files = process_run(owner, repo, run, bot_token, chat_id, dry_run)
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {error.code} while processing run {run_id}: {details}") from error

        log(f"Completed run {run_id} with {len(sent_files)} file(s).")
        if dry_run:
            continue

        history.append(
            {
                "run_id": run_id,
                "branch": run.get("head_branch"),
                "head_sha": run.get("head_sha"),
                "html_url": run.get("html_url"),
                "forwarded_files": sent_files,
            }
        )
        history = history[-20:]
        state = {
            "last_processed_run_id": run_id,
            "history": history,
        }
        save_state(state_path, state)

    if dry_run:
        log("Dry run finished without sending files or writing state.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"ERROR: {error}")
        raise
