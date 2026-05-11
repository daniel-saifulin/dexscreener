"""Управление подпиской на Helius webhook'ы.

CLI:
    python -m dexbot.setup_helius_webhook list
    python -m dexbot.setup_helius_webhook create --url https://<app>.fly.dev/webhook/helius
    python -m dexbot.setup_helius_webhook update WEBHOOK_ID --url ...
    python -m dexbot.setup_helius_webhook delete WEBHOOK_ID

Подписываем Helius на наши core-кошельки. Когда они делают swap on-chain,
Helius шлёт POST на наш fly.io-эндпоинт.

Helius webhook API: https://docs.helius.dev/webhooks-and-websockets/webhooks
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
import requests
from dotenv import load_dotenv

HELIUS_BASE = "https://api.helius.xyz"


def _key() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        print("ERROR: HELIUS_API_KEY not set in env", file=sys.stderr)
        sys.exit(2)
    return key


def _core_addresses() -> list[str]:
    db = os.environ.get("DATABASE_URL")
    if not db:
        print("ERROR: DATABASE_URL not set in env", file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT address FROM watched_wallets "
            "WHERE is_core=TRUE AND is_active=TRUE "
            "ORDER BY score DESC NULLS LAST"
        )
        return [row[0] for row in cur.fetchall()]


def cmd_list() -> None:
    r = requests.get(f"{HELIUS_BASE}/v0/webhooks", params={"api-key": _key()}, timeout=15)
    r.raise_for_status()
    hooks = r.json() or []
    if not hooks:
        print("(нет активных webhook'ов)")
        return
    for h in hooks:
        print(f"  id          : {h.get('webhookID')}")
        print(f"  url         : {h.get('webhookURL')}")
        print(f"  type        : {h.get('webhookType')}")
        print(f"  tx_types    : {h.get('transactionTypes')}")
        addrs = h.get('accountAddresses') or []
        print(f"  addresses   : {len(addrs)} кошельков")
        for a in addrs[:3]:
            print(f"                 {a}")
        if len(addrs) > 3:
            print(f"                 ... и ещё {len(addrs) - 3}")
        print()


def cmd_create(url: str, secret: str | None = None) -> None:
    addrs = _core_addresses()
    if not addrs:
        print("ERROR: нет core-кошельков в БД", file=sys.stderr)
        sys.exit(2)
    print(f"Подписываемся на {len(addrs)} core-кошельков:")
    for a in addrs:
        print(f"  {a}")
    body: dict = {
        "webhookURL": url,
        "transactionTypes": ["SWAP"],
        "accountAddresses": addrs,
        "webhookType": "enhanced",
    }
    if secret:
        body["authHeader"] = secret
    r = requests.post(
        f"{HELIUS_BASE}/v0/webhooks",
        params={"api-key": _key()},
        json=body,
        timeout=20,
    )
    if not r.ok:
        print(f"ERROR {r.status_code}: {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    data = r.json()
    print(f"\nWebhook создан: id={data.get('webhookID')}")
    print(f"URL: {data.get('webhookURL')}")


def cmd_update(webhook_id: str, url: str | None = None, secret: str | None = None) -> None:
    addrs = _core_addresses()

    # Helius PUT требует webhookURL всегда. Если не передан — берём текущий из самого webhook'а.
    if not url:
        r = requests.get(
            f"{HELIUS_BASE}/v0/webhooks/{webhook_id}",
            params={"api-key": _key()},
            timeout=15,
        )
        if not r.ok:
            print(f"ERROR fetching current webhook: {r.status_code} {r.text[:300]}",
                  file=sys.stderr)
            sys.exit(1)
        current = r.json()
        url = current.get("webhookURL")
        if not url:
            print("ERROR: webhook не имеет URL в текущем состоянии и --url не передан",
                  file=sys.stderr)
            sys.exit(1)
        print(f"(URL не передан, использую текущий: {url})")

    body: dict = {
        "webhookURL": url,
        "transactionTypes": ["SWAP"],
        "accountAddresses": addrs,
        "webhookType": "enhanced",
    }
    if secret:
        body["authHeader"] = secret

    r = requests.put(
        f"{HELIUS_BASE}/v0/webhooks/{webhook_id}",
        params={"api-key": _key()},
        json=body,
        timeout=20,
    )
    if not r.ok:
        print(f"ERROR {r.status_code}: {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    print(f"Webhook {webhook_id} обновлён. Подписано: {len(addrs)} адресов.")


def cmd_delete(webhook_id: str) -> None:
    r = requests.delete(
        f"{HELIUS_BASE}/v0/webhooks/{webhook_id}",
        params={"api-key": _key()},
        timeout=20,
    )
    if not r.ok:
        print(f"ERROR {r.status_code}: {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    print(f"Webhook {webhook_id} удалён.")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="setup_helius_webhook")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Показать текущие webhook'и.")

    p_create = sub.add_parser("create", help="Создать новый webhook.")
    p_create.add_argument("--url", required=True, help="https://<app>.fly.dev/webhook/helius")
    p_create.add_argument("--secret", help="HELIUS_WEBHOOK_SECRET (опционально).")

    p_update = sub.add_parser("update", help="Обновить (например, список адресов после изменения ядра).")
    p_update.add_argument("webhook_id")
    p_update.add_argument("--url")
    p_update.add_argument("--secret")

    p_del = sub.add_parser("delete", help="Удалить webhook.")
    p_del.add_argument("webhook_id")

    args = p.parse_args(argv)
    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "create":
        cmd_create(args.url, args.secret)
    elif args.cmd == "update":
        cmd_update(args.webhook_id, args.url, args.secret)
    elif args.cmd == "delete":
        cmd_delete(args.webhook_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
