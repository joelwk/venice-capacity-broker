import json
import re
import threading
import time
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import (  # type: ignore[import-not-found]
    Error as PlaywrightError,
)
from playwright.sync_api import (
    expect,
    sync_playwright,
)

CONTROL_PLANE_DIR = Path(__file__).resolve().parents[2] / "apps" / "control-plane"
TREASURY_ADDRESS = "0xCAFEBABE00000000000000000000000000000001"
REL_TOL = 1e-6
EXPECTED_USDC_AMOUNT = 22.66
EXPECTED_CLIPBOARD_WRITES = 3
QUOTE_ROWS = 3


class ControlPlaneHandler(SimpleHTTPRequestHandler):
    """Static asset handler with minimal API stubs for deterministic tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CONTROL_PLANE_DIR), **kwargs)

    def log_message(self, _fmt: str, *_args) -> None:  # pragma: no cover
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/buy.html"}:
            self.path = "/buy.html"
            super().do_GET()
            return

        payload = self._stub_get_payload(path, parsed)
        if payload is not None:
            self._send_json(payload)
            return

        super().do_GET()

    def _stub_get_payload(self, path: str, parsed) -> dict | list | None:
        if path == "/v1/env":
            now = int(time.time())
            return {
                "payments": {
                    "treasury_address": TREASURY_ADDRESS,
                    "accepted_assets": ["USDC"],
                    "usdc_address": "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                },
                "features": {
                    "quotes": True,
                    "purchases": True,
                    "clearing": False,
                    "bids": False,
                },
                "buyer": {"quote_ttl": 90, "last_updated": now},
            }

        if path == "/v1/quotes":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["USDC"])[0]
            units = float(params.get("units", ["0.10"])[0])
            return {
                "quoteId": "q-demo",
                "asset": asset,
                "units": units,
                "unitPrice": 226600000,
                "totalPrice": 22660000,
                "acceptedMin": 0.01,
                "acceptedMax": 1000,
                "expiresAt": int(time.time()) + 90,
            }

        if path == "/v1/market/prices":
            return {
                "prices": {"USDC": 1.0, "DIEM": 226.6, "ETH": 3000.0},
                "ratios": {},
            }

        if path in {"/v1/tenants", "/v1/bids"}:
            return []

        if path in {
            "/v1/purchases/challenge",
            "/v1/purchases/recover/challenge",
        }:
            params = parse_qs(parsed.query)
            tx_hash = params.get("txHash", [""])[0]
            buyer = params.get("buyerAddress", [""])[0]
            return {
                "message": (
                    "Venice Capacity Broker Wallet Verification\n"
                    f"Transaction: {tx_hash}\n"
                    f"Buyer: {buyer}\n"
                    "Nonce: demo-nonce\n"
                    "Expires: 2099-01-01T00:00:00Z"
                ),
                "nonce": "demo-nonce",
                "expiresAt": "2099-01-01T00:00:00Z",
                "txHash": tx_hash,
                "buyerAddress": buyer,
            }

        return None

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/purchases/verify":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send_json(
                {
                    "subkey": "demo-subkey-123",
                    "quoteId": body.get("quoteId"),
                    "txHash": body.get("txHash"),
                }
            )
            return None

        return super().do_POST()


@contextmanager
def serve_control_plane() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.e2e
def test_quote_to_key_happy_path():  # noqa: PLR0915
    with serve_control_plane() as base_url, sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Playwright browser launch failed: {exc}")

        try:
            page = browser.new_page()
            page.add_init_script(
                """
                (() => {
                  window.__clipboardWrites = [];
                  navigator.clipboard = {
                    writeText: async (text) => { window.__clipboardWrites.push(text); }
                  };
                  window.ethereum = {
                    request: async ({ method }) => {
                      if (method === 'eth_requestAccounts') {
                        return ['0xDEADBEEF00000000000000000000000000000001'];
                      }
                      if (method === 'personal_sign') {
                        return '0x' + 'ab'.repeat(65);
                      }
                      if (method === 'wallet_switchEthereumChain') {
                        return null;
                      }
                      if (method === 'eth_sendTransaction') {
                        return '0xTRANSACTIONHASH';
                      }
                      return null;
                    }
                  };
                })();
                """
            )

            page.goto(f"{base_url}/buy.html")
            page.wait_for_load_state("networkidle")

            page.wait_for_selector("#pricing-table:not(.hidden)")
            expect(page.locator("#pricing-tbody tr")).to_have_count(QUOTE_ROWS)
            expect(
                page.locator("#pricing-tbody tr").nth(0).locator("td").first()
            ).to_have_text("DIEM")
            expect(page.locator("#pricing-note")).to_have_text(
                "Generate a quote to compare mint pricing against the market."
            )

            # Step 1 - request a quote
            page.get_by_role("button", name="Get Quote").click()
            page.wait_for_selector("#quote-details:not(.hidden)")

            expect(page.locator("#quote-status")).to_have_text(
                "Quote ready. Send the payment before it expires."
            )
            expect(page.locator("#quote-amount")).to_have_value(re.compile("USDC"))
            expect(page.locator("#quote-address")).to_have_value(TREASURY_ADDRESS)

            page.locator("#copy-amount").click()
            page.wait_for_function("() => window.__clipboardWrites.length === 1")
            expect(page.locator("#quote-status")).to_have_text(
                "Amount copied to clipboard."
            )

            page.locator("#copy-address").click()
            page.wait_for_function("() => window.__clipboardWrites.length === 2")
            expect(page.locator("#quote-status")).to_have_text(
                "Treasury address copied."
            )

            page.wait_for_function(
                "() => document.getElementById('pricing-note')."
                "textContent.includes('Latest quote')"
            )
            expect(page.locator("#pricing-note")).to_contain_text("Latest quote (USDC)")
            expect(
                page.locator("#pricing-tbody tr").nth(1).locator("td").nth(3)
            ).to_have_text("0.00%")

            page.wait_for_function(
                "() => !document.getElementById('step-verify')."
                "classList.contains('step-disabled')"
            )

            page.locator("#connect-wallet").click()
            expect(page.locator("#wallet-address")).to_have_value(
                re.compile("^0xdeadbeef", re.IGNORECASE)
            )
            expect(page.locator("#verify-status")).to_contain_text("Wallet connected")

            tx_hash = "0x" + "a" * 64
            page.fill("#tx-hash", tx_hash)
            page.wait_for_function(
                "() => !document.getElementById('verify-btn').disabled"
            )

            page.get_by_role("button", name="Verify Payment").click()
            expect(page.locator("#verify-status")).to_contain_text("Payment verified")

            page.wait_for_selector("#step-key:not(.step-hidden)")
            expect(page.locator("#api-key")).to_have_value("demo-subkey-123")
            expect(page.locator("#key-status")).to_contain_text("API key issued")

            page.locator("#copy-key").click()
            page.wait_for_function(
                f"() => window.__clipboardWrites.length === {EXPECTED_CLIPBOARD_WRITES}"
            )
            expect(page.locator("#key-status")).to_have_text(
                "API key copied to clipboard."
            )

            clipboard_writes = page.evaluate("window.__clipboardWrites")
            amount_text = clipboard_writes[0]
            assert amount_text.endswith("USDC")
            assert float(amount_text.split()[0]) == pytest.approx(
                EXPECTED_USDC_AMOUNT, rel=REL_TOL
            )
            assert clipboard_writes[1] == TREASURY_ADDRESS
            assert clipboard_writes[2] == "demo-subkey-123"
        finally:
            browser.close()
