"""main 实验包仅交付本人私有仓库；不创建仓库、不公开发布。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import httpx


def verify_owner_only(client, repository, owner):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("private delivery repository is not configured")
    identity = client.get("https://api.github.com/user")
    identity.raise_for_status()
    if identity.json().get("login", "").lower() != owner.lower():
        raise ValueError("private delivery token does not belong to the owner")
    response = client.get(f"https://api.github.com/repos/{repository}")
    response.raise_for_status()
    repo = response.json()
    if repo.get("private") is not True or repo.get("owner", {}).get("type") != "User" or repo["owner"].get("login", "").lower() != owner.lower():
        raise ValueError("experimental packages require the user's personal private repository")
    for page in range(1, 101):
        response = client.get(f"https://api.github.com/repos/{repository}/collaborators", params={"per_page": 100, "page": page})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or any(row.get("login", "").lower() != owner.lower() for row in rows):
            raise ValueError("private delivery repository grants access to another user")
        if len(rows) < 100:
            return repo
    raise ValueError("cannot verify complete private access list")


def deliver(candidate, *, client, repository, owner, source_commit):
    receipt_path = candidate / "candidate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archive = candidate / "KnowledgeRadar.zip"
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit) or receipt.get("source_commit") != source_commit or receipt.get("source_dirty") is not False:
        raise ValueError("candidate source identity mismatch")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != receipt.get("archive_sha256"):
        raise ValueError("candidate archive identity mismatch")
    ci = receipt.get('ci', {})
    version = receipt.get('version', '')
    if (not re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,99}', version)
            or ci.get('event_name') != 'push' or ci.get('ref') != 'refs/heads/main'
            or not re.fullmatch(r'[1-9][0-9]*', str(ci.get('run_id', '')))
            or not re.fullmatch(r'[1-9][0-9]*', str(ci.get('run_attempt', '')))
            or not re.fullmatch(re.escape(owner) + r'/[A-Za-z0-9_.-]+', ci.get('repository', ''), re.I)):
        raise ValueError('candidate main CI identity missing')
    tag = f"internal-v{version}-r{ci['run_id']}.{ci['run_attempt']}"
    checksum = candidate / f'KnowledgeRadar-{version}-SHA256SUMS.txt'
    contents = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
                       for p in (archive, receipt_path))
    if checksum.exists() and checksum.read_text(encoding='utf-8') != contents:
        raise ValueError('candidate checksum identity mismatch')
    if not checksum.exists():
        checksum.write_text(contents, encoding='utf-8', newline='\n')
    verify_owner_only(client, repository, owner)
    response = client.get(f"https://api.github.com/repos/{repository}/releases/tags/{tag}")
    if response.status_code == 404:
        response = client.post(f"https://api.github.com/repos/{repository}/releases", json={
            'tag_name': tag, 'target_commitish': 'main',
            'name': f"内部试用 v{version} · 构建 r{ci['run_id']}.{ci['run_attempt']}",
            'prerelease': True, 'draft': True,
            'body': f"仅本人试用；不代表允许公开发布。\n源码：{source_commit}\nZIP SHA-256：{receipt['archive_sha256']}\n"
                    f"CI：https://github.com/{ci['repository']}/actions/runs/{ci['run_id']} (attempt {ci['run_attempt']})"})
    response.raise_for_status()
    release = response.json()
    if release.get("tag_name") != tag or release.get("prerelease") is not True or type(release.get('draft')) is not bool:
        raise ValueError("existing experimental release identity mismatch")
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid experimental release id")
    upload = str(release.get("upload_url", "")).split("{", 1)[0]
    parsed = urlsplit(upload)
    if parsed.scheme != "https" or parsed.hostname != "uploads.github.com" or parsed.path != f"/repos/{repository}/releases/{release_id}/assets" or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("unexpected GitHub upload host")
    # 上传前再次核权限；不回退为公开Actions artifact或公开prerelease。
    verify_owner_only(client, repository, owner)
    assets = []
    for page in range(1, 101):
        response = client.get(f"https://api.github.com/repos/{repository}/releases/{release_id}/assets", params={"per_page":100,"page":page})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows,list):
            raise ValueError("invalid experimental assets")
        assets.extend(rows)
        if len(rows) < 100:
            break
    else:
        raise ValueError("cannot verify complete asset list")
    for path in (archive, receipt_path, checksum):
        expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        existing = [asset for asset in assets if asset.get("name") == path.name]
        if existing:
            if len(existing) != 1 or existing[0].get("digest") != expected or existing[0].get("size") != path.stat().st_size:
                raise ValueError("existing experimental asset identity mismatch")
            continue
        if not release['draft']:
            raise ValueError('published internal release is incomplete; will not append or overwrite assets')
        with path.open("rb") as stream:
            response = client.post(upload, params={"name": path.name}, content=stream,
                headers={"Content-Type": "application/zip" if path.suffix == ".zip" else "application/json" if path.suffix == '.json' else 'text/plain'})
        response.raise_for_status()
        uploaded = response.json()
        if uploaded.get("digest") != expected or uploaded.get("size") != path.stat().st_size:
            raise ValueError("uploaded experimental asset identity mismatch")
    verify_owner_only(client, repository, owner)
    if release['draft']:
        response = client.patch(f'https://api.github.com/repos/{repository}/releases/{release_id}', json={'draft': False})
        response.raise_for_status()
        published = response.json()
        if published.get('draft') is not False or published.get('tag_name') != tag or published.get('prerelease') is not True:
            raise ValueError('internal release publish identity mismatch')
    return {"status": "private_experiment_delivered", "source_commit": source_commit,
            "archive_sha256": receipt["archive_sha256"], "repository": repository, "public_release": False,
            'tag': tag, 'version': version, 'ci': ci}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("only main push can deliver an experimental package")
    owner = os.environ.get("KR_DELIVERY_OWNER", "")
    repository = os.environ.get("KR_PRIVATE_DELIVERY_REPOSITORY", "")
    token = os.environ.get("KR_PRIVATE_DELIVERY_TOKEN", "")
    if not owner or not repository or not token:
        raise ValueError("owner-only private delivery is not configured; package not distributed")
    receipt = json.loads((args.candidate/'candidate-receipt.json').read_text(encoding='utf-8'))
    for key, variable in [('repository', 'GITHUB_REPOSITORY'), ('run_id', 'GITHUB_RUN_ID'), ('run_attempt', 'GITHUB_RUN_ATTEMPT')]:
        if receipt.get('ci', {}).get(key) != os.environ.get(variable):
            raise ValueError('candidate CI identity differs from delivery workflow')
    with httpx.Client(headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, follow_redirects=False, timeout=120) as client:
        result = deliver(args.candidate, client=client, repository=repository, owner=owner, source_commit=os.environ.get("GITHUB_SHA", ""))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # 不把含认证请求头的异常对象、响应正文打印到公开CI日志。
        raise SystemExit("private delivery blocked: " + type(exc).__name__)
