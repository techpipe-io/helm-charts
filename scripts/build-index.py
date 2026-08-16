#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def fail(message: str) -> None:
    raise RuntimeError(message)


def require_http_url(value: str, what: str) -> str:
    parsed = urlparse(value)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        fail(f"{what} must be an absolute http(s) URL: {value!r}")

    return value


def fetch_yaml(url: str, attempts: int = 3, timeout: int = 30):
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "helm-index-aggregator/1.0",
                    "Accept": "application/yaml,text/yaml,text/plain,*/*",
                },
            )

            with urlopen(request, timeout=timeout) as response:
                return yaml.safe_load(response.read())

        except (HTTPError, URLError, TimeoutError, yaml.YAMLError) as exc:
            last_error = exc

            if attempt < attempts:
                time.sleep(attempt * 2)

    fail(
        f"failed to fetch {url} after "
        f"{attempts} attempts: {last_error}"
    )


def resolve_chart_url(index_url: str, chart_url: str) -> str:
    resolved = urljoin(index_url, chart_url)
    return require_http_url(resolved, "chart URL")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build one Helm index.yaml from multiple "
            "public Helm repositories."
        )
    )

    parser.add_argument(
        "--config",
        default="repositories.yml",
        help="Path to repositories.yml",
    )

    parser.add_argument(
        "--output",
        default="public/index.yaml",
        help="Output aggregate index.yaml",
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    output_path = Path(args.output)

    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    repositories = config.get("repositories")

    if not isinstance(repositories, list) or not repositories:
        fail(
            "repositories.yml must contain "
            "a non-empty 'repositories' list"
        )

    merged_entries: dict[str, list[dict]] = {}
    chart_owner: dict[str, str] = {}
    seen_versions: set[tuple[str, str]] = set()
    repo_names: set[str] = set()

    total_versions = 0

    for repository in repositories:
        if not isinstance(repository, dict):
            fail(
                f"repository entry must be a mapping: "
                f"{repository!r}"
            )

        name = repository.get("name")
        base_url = repository.get("url")

        if not isinstance(name, str) or not name.strip():
            fail(
                f"repository entry has invalid name: "
                f"{repository!r}"
            )

        if name in repo_names:
            fail(f"duplicate repository name: {name}")

        repo_names.add(name)

        if not isinstance(base_url, str) or not base_url.strip():
            fail(f"repository {name!r} has invalid url")

        base_url = require_http_url(
            base_url.strip(),
            f"repository {name} URL",
        )

        if not base_url.endswith("/"):
            base_url += "/"

        index_url = repository.get("index_url")

        if index_url is None:
            index_url = urljoin(base_url, "index.yaml")

        elif not isinstance(index_url, str) or not index_url.strip():
            fail(f"repository {name!r} has invalid index_url")

        else:
            index_url = require_http_url(
                index_url.strip(),
                f"repository {name} index_url",
            )

        print(
            f"[{name}] fetching {index_url}",
            file=sys.stderr,
        )

        source_index = fetch_yaml(index_url)

        if not isinstance(source_index, dict):
            fail(
                f"repository {name!r}: "
                "index.yaml is not a YAML mapping"
            )

        if source_index.get("apiVersion") != "v1":
            fail(
                f"repository {name!r}: unsupported apiVersion "
                f"{source_index.get('apiVersion')!r}"
            )

        entries = source_index.get("entries")

        if not isinstance(entries, dict):
            fail(
                f"repository {name!r}: "
                "index.yaml has no valid entries mapping"
            )

        for chart_name, versions in entries.items():
            if not isinstance(chart_name, str) or not chart_name:
                fail(
                    f"repository {name!r}: "
                    f"invalid chart name {chart_name!r}"
                )

            if not isinstance(versions, list) or not versions:
                fail(
                    f"repository {name!r}: "
                    f"chart {chart_name!r} "
                    "has no version entries"
                )

            previous_owner = chart_owner.get(chart_name)

            if (
                previous_owner is not None
                and previous_owner != name
            ):
                fail(
                    f"chart {chart_name!r} exists in more "
                    "than one source repository: "
                    f"{previous_owner!r} and {name!r}"
                )

            chart_owner[chart_name] = name

            target_versions = merged_entries.setdefault(
                chart_name,
                [],
            )

            for version_entry in versions:
                if not isinstance(version_entry, dict):
                    fail(
                        f"repository {name!r}: "
                        f"invalid entry for chart "
                        f"{chart_name!r}"
                    )

                entry_name = version_entry.get("name")

                if (
                    entry_name is not None
                    and entry_name != chart_name
                ):
                    fail(
                        f"repository {name!r}: "
                        f"entry key {chart_name!r} "
                        f"does not match name {entry_name!r}"
                    )

                version = version_entry.get("version")

                if not isinstance(version, str) or not version:
                    fail(
                        f"repository {name!r}: "
                        f"chart {chart_name!r} "
                        f"has invalid version {version!r}"
                    )

                key = (chart_name, version)

                if key in seen_versions:
                    fail(
                        "duplicate chart version in aggregate "
                        f"index: {chart_name}-{version}"
                    )

                seen_versions.add(key)

                urls = version_entry.get("urls")

                if not isinstance(urls, list) or not urls:
                    fail(
                        f"repository {name!r}: "
                        f"{chart_name}-{version} "
                        "has no download URLs"
                    )

                normalized_entry = copy.deepcopy(
                    version_entry
                )

                normalized_entry["urls"] = [
                    resolve_chart_url(
                        index_url,
                        chart_url,
                    )
                    for chart_url in urls
                    if (
                        isinstance(chart_url, str)
                        and chart_url
                    )
                ]

                if len(normalized_entry["urls"]) != len(urls):
                    fail(
                        f"repository {name!r}: "
                        f"{chart_name}-{version} "
                        "contains an invalid chart URL"
                    )

                target_versions.append(
                    normalized_entry
                )

                total_versions += 1

    aggregate = {
        "apiVersion": "v1",
        "entries": {
            chart_name: merged_entries[chart_name]
            for chart_name in sorted(merged_entries)
        },
        "generated": (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
    }

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = output_path.with_suffix(
        output_path.suffix + ".tmp"
    )

    with tmp_path.open("w", encoding="utf-8") as fh:
        yaml.dump(
            aggregate,
            fh,
            Dumper=NoAliasDumper,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

    os.replace(tmp_path, output_path)

    print(
        f"built {output_path}: "
        f"{len(merged_entries)} charts, "
        f"{total_versions} versions, "
        f"{len(repositories)} repositories",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except RuntimeError as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
