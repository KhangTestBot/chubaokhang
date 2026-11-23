import base64
from email.utils import formatdate
import json
import unittest

from backend_py.server import Config, Response, create_server, create_vercel_handler, read_config
from backend_py.storage import VercelBlobImageStore, parse_vercel_blob_store_id
from test_python.test_backend import (
    FAKE_ACCESS_TOKEN, SOURCE as VALID_PNG, call, edit_request, public_resolver, running,
)


# Minimal valid JPEG (SOI + APP0 + EOI) recognized by storage.image_type
VALID_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
SAMPLE_BLOB_TOKEN = "vercel_blob_rw_AbCdEf1234567890_secretTokenValue123456"


class CloudflareProviderTests(unittest.TestCase):
    def setUp(self):
        self.token = FAKE_ACCESS_TOKEN
        self.env = {
            "PORT": "0",
            "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
            "CLOUDFLARE_API_TOKEN": "cfut_test_token_value_1234567890",
            "BACKEND_ACCESS_TOKEN": self.token,
            "VERCEL_PROJECT_PRODUCTION_URL": "chubaokhang.vercel.app",
        }

    def test_read_config_auto_selects_cloudflare_and_vercel_url(self):
        cfg = read_config(self.env)
        self.assertEqual(cfg.provider, "cloudflare")
        self.assertEqual(cfg.cloudflare_model, "@cf/black-forest-labs/flux-2-klein-4b")
        self.assertEqual(cfg.public_base_url, "https://chubaokhang.vercel.app")
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.composition_configured)

    def test_read_config_rejects_invalid_provider_and_account_id(self):
        with self.assertRaises(ValueError):
            read_config({**self.env, "IMAGE_PROVIDER": "unknown"})
        with self.assertRaises(ValueError):
            read_config({**self.env, "CLOUDFLARE_ACCOUNT_ID": "not-hex!"})
        with self.assertRaises(ValueError):
            read_config({**self.env, "CLOUDFLARE_IMAGE_MODEL": "../bad"})

    def test_cloudflare_edit_end_to_end_multipart_and_jpeg_output(self):
        cfg = read_config(self.env)
        captured = {}

        def fake_fetch(url, *, method, headers, body, maximum, timeout, addresses=None):
            if url.startswith("https://cdn.locketcamera.com/"):
                return Response(200, {"content-type": "image/png"}, VALID_PNG)
            captured["url"] = url
            captured["method"] = method
            captured["headers"] = headers
            captured["body"] = body
            encoded = base64.b64encode(VALID_JPEG).decode("ascii")
            return Response(200, {"content-type": "application/json"},
                            json.dumps({"success": True, "result": {"image": encoded}}).encode("utf-8"))

        with running(cfg, fetch=fake_fetch, resolver=public_resolver) as server:
            status, _, body = edit_request(server)
            self.assertEqual(status, 200, body)
            payload = json.loads(body)
            self.assertTrue(payload["outputUrl"].startswith("https://chubaokhang.vercel.app/images/"))
            self.assertTrue(payload["outputUrl"].endswith(".jpg"))

            self.assertEqual(
                captured["url"],
                f"https://api.cloudflare.com/client/v4/accounts/{'a' * 32}/ai/run/@cf/black-forest-labs/flux-2-klein-4b",
            )
            self.assertEqual(captured["method"], "POST")
            self.assertEqual(captured["headers"]["Authorization"], "Bearer cfut_test_token_value_1234567890")
            self.assertTrue(captured["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
            self.assertIn(b'name="prompt"', captured["body"])
            self.assertIn(b'name="input_image_0"', captured["body"])
            self.assertIn(VALID_PNG, captured["body"])

            image_path = payload["outputUrl"].replace("https://chubaokhang.vercel.app", "")
            img_status, img_headers, img_body = call(server, "GET", image_path)
            self.assertEqual(img_status, 200)
            self.assertEqual({k.lower(): v for k, v in img_headers.items()}["content-type"], "image/jpeg")
            self.assertEqual(img_body, VALID_JPEG)

    def test_firebase_default_port_443_image_url_is_accepted(self):
        from backend_py.server import validate_input_url
        cfg = read_config(self.env)
        validated = validate_input_url(
            "https://firebasestorage.googleapis.com:443/v0/b/locket-img/o/photo.jpg?alt=media",
            cfg,
            resolver=public_resolver,
        )
        self.assertEqual(validated.hostname, "firebasestorage.googleapis.com")
        self.assertEqual(validated.url, "https://firebasestorage.googleapis.com/v0/b/locket-img/o/photo.jpg?alt=media")

    def test_cloudflare_prompt_optimizer_locks_face_and_rewrites_flagged_prompts(self):
        from backend_py.server import _optimize_cloudflare_prompt
        realistic = _optimize_cloudflare_prompt("Add a red rose in their mouth.")
        self.assertIn("Strictly preserve the person's exact face", realistic)
        stylized = _optimize_cloudflare_prompt("Make everything look like Minecraft 3D.")
        self.assertIn("recognizable facial likeness", stylized)
        self.assertNotIn("Strictly preserve the person's exact face", stylized)
        hero = _optimize_cloudflare_prompt("Suit them up like a superhero in a blue and red outfit with a flowing cape.")
        self.assertNotIn("superhero", hero.lower())
        self.assertIn("crimson red cape", hero.lower())
        ogre = _optimize_cloudflare_prompt("Transform them into Shrek with rounded ears.")
        self.assertNotIn("shrek", ogre.lower())
        self.assertIn("green ogre", ogre.lower())

    def test_cloudflare_safety_block_returns_422(self):
        cfg = read_config(self.env)

        def fake_fetch(url, *, method, headers, body, maximum, timeout, addresses=None):
            if url.startswith("https://cdn.locketcamera.com/"):
                return Response(200, {"content-type": "image/png"}, VALID_PNG)
            return Response(
                400,
                {"content-type": "application/json"},
                json.dumps({"success": False, "errors": [{"code": 3030, "message": "Your output has been flagged."}]}).encode(),
            )

        with running(cfg, fetch=fake_fetch, resolver=public_resolver) as server:
            status, _, body = edit_request(server)
            self.assertEqual(status, 422, body)
            self.assertEqual(json.loads(body)["error"]["code"], "generation_blocked")


class VercelBlobStorageTests(unittest.TestCase):
    def test_parse_store_id(self):
        self.assertEqual(parse_vercel_blob_store_id(SAMPLE_BLOB_TOKEN), "abcdef1234567890")
        with self.assertRaises(ValueError):
            parse_vercel_blob_store_id("invalid-token")

    def test_save_read_and_expire_on_vercel_blob(self):
        clock = [1_800_000_000.0]
        blobs = {}
        deletes = []

        def fake_fetch(url, *, method, headers, body, maximum, timeout, addresses=None):
            if method == "PUT" and url.startswith("https://blob.vercel-storage.com/locket-generated/"):
                pathname = url.split("https://blob.vercel-storage.com/", 1)[1]
                self.assertEqual(headers["x-add-random-suffix"], "0")
                self.assertEqual(headers["x-vercel-blob-access"], "public")
                blobs[pathname] = (body, clock[0])
                return Response(200, {"content-type": "application/json"}, b'{"url":"https://example.com"}')
            if method == "GET" and url.startswith("https://abcdef1234567890.public.blob.vercel-storage.com/"):
                pathname = url.split(".com/", 1)[1]
                if pathname not in blobs:
                    return Response(404, {}, b"not found")
                data, ts = blobs[pathname]
                return Response(200, {"last-modified": formatdate(ts, usegmt=True)}, data)
            if method == "POST" and url == "https://blob.vercel-storage.com/delete":
                deletes.append(json.loads(body))
                return Response(200, {"content-type": "application/json"}, b"{}")
            raise AssertionError(f"Unexpected fetch: {method} {url}")

        store = VercelBlobImageStore(SAMPLE_BLOB_TOKEN, ttl_seconds=600, now=lambda: clock[0], fetch=fake_fetch)
        filename = store.save(VALID_JPEG, "image/jpeg")
        self.assertTrue(filename.endswith(".jpg"))

        result = store.read(filename)
        self.assertEqual(result, (VALID_JPEG, "image/jpeg"))

        clock[0] += 601
        self.assertIsNone(store.read(filename))
        self.assertEqual(len(deletes), 1)
        self.assertIn(filename, deletes[0]["urls"][0])

    def test_vercel_handler_entrypoint_builds_cleanly(self):
        from http.server import BaseHTTPRequestHandler
        import api.index as vercel_entry

        self.assertTrue(issubclass(vercel_entry.handler, BaseHTTPRequestHandler))
        custom = create_vercel_handler(env={})
        self.assertTrue(issubclass(custom, BaseHTTPRequestHandler))


if __name__ == "__main__":
    unittest.main()
