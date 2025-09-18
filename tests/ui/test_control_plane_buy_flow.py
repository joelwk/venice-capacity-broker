import json
import threading
import time
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # type: ignore  # noqa: E402

CONTROL_PLANE_DIR = Path(__file__).resolve().parents[2] / "apps" / "control-plane"
TREASURY_ADDRESS = "0xCAFEBABE00000000000000000000000000000001"


class ControlPlaneHandler(SimpleHTTPRequestHandler):
    """Static asset handler with minimal API stubs for deterministic tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CONTROL_PLANE_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:  # pragma: no cover
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/buy.html"}:
            self.path = "/buy.html"
            return super().do_GET()

        if path == "/v1/env":
            now = int(time.time())
            self._send_json(
                {
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
            )
            return

        if path == "/v1/quotes":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["USDC"])[0]
            units = float(params.get("units", ["0.10"])[0])
            self._send_json(
                {
                    "quoteId": "q-demo",
                    "asset": asset,
                    "units": units,
                    "unitPrice": 226600000,
                    "totalPrice": 22660000,
                    "acceptedMin": 0.01,
                    "acceptedMax": 1000,
                    "expiresAt": int(time.time()) + 90,
                }
            )
            return

        if path == "/v1/market/prices":
            self._send_json({"prices": {"USDC": 1.0, "DIEM": 226.6, "ETH": 3000.0}, "ratios": {}})
            return

        if path in {"/v1/tenants", "/v1/bids"}:
            self._send_json([])
            return

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
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
            return

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
def test_quote_to_key_happy_path():
    with serve_control_plane() as base_url:
        with sync_playwright() as p:
            browser = p.chromium.launch()
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

                page.get_by_role("button", name="Connect Wallet").click()
                expect(page.locator("#walletStatus")).to_contain_text("Connected:")

                page.get_by_role("button", name="Get Quote").click()
                page.wait_for_function(
                    "() => !document.getElementById('quoteAdvanceBtn').classList.contains('hide')"
                )

                page.get_by_role("button", name="Continue to Pay & Verify").click()
                page.wait_for_function(
                    "() => !document.getElementById('payFlow').classList.contains('hide')"
                )

                expect(page.locator("#paySummary")).to_contain_text("Send")
                expect(page.locator("#paySummary")).to_contain_text(TREASURY_ADDRESS)

                page.locator("#copyAddressBtn").click()
                page.wait_for_function("() => window.__clipboardWrites.length === 1")
                expect(page.locator("#payStatus")).to_have_text("Treasury address copied.")

                page.locator("#copyAmountBtn").click()
                page.wait_for_function("() => window.__clipboardWrites.length === 2")
                expect(page.locator("#payStatus")).to_contain_text("copied")

                page.fill("#txHash", "0xfeedface")
                page.get_by_role("button", name="Verify payment").click()

                expect(page.locator("#verifyMsg")).to_contain_text("Key issued")
                expect(page.locator("#keyOut")).to_contain_text("demo-subkey-123")

                page.wait_for_function(
                    "() => !document.getElementById('copyKeyBtn').classList.contains('hide')"
                )
                page.get_by_role("button", name="Copy key").click()
                page.wait_for_function("() => window.__clipboardWrites.length === 3")
                expect(page.locator("#verifyMsg")).to_contain_text("copied")

                clipboard_writes = page.evaluate("window.__clipboardWrites")
                assert clipboard_writes[0] == TREASURY_ADDRESS
                assert float(clipboard_writes[1]) == pytest.approx(22.66, rel=1e-6)
                assert clipboard_writes[2] == "demo-subkey-123"
            finally:
                browser.close()
