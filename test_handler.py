from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import handler


def write_glb(path: Path, document: dict) -> None:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    length = 12 + 8 + len(payload)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, length)
        + struct.pack("<II", len(payload), handler.JSON_CHUNK)
        + payload
    )


class HandlerTest(unittest.TestCase):

    def test_validate_job_input_accepts_expected_key(self) -> None:
        with patch.dict("os.environ", {"MODEL_CONVERT_OUTPUT_PREFIX": "model-convert/output"}):
            value = handler.validate_job_input(
                {
                    "input": {
                        "task_id": "123",
                        "source_url": "https://cdn.example.com/input.glb?signature=secret",
                        "output_key": "model-convert/output/123.glb",
                    }
                }
            )
        self.assertEqual(
            value,
            (
                "123",
                "https://cdn.example.com/input.glb?signature=secret",
                "model-convert/output/123.glb",
            ),
        )

    def test_validate_job_input_rejects_arbitrary_output_key(self) -> None:
        with self.assertRaisesRegex(handler.ModelConvertError, "output_key"):
            handler.validate_job_input(
                {
                    "input": {
                        "task_id": "123",
                        "source_url": "https://cdn.example.com/input.glb",
                        "output_key": "someone-else/file.glb",
                    }
                }
            )

    def test_input_validator_accepts_glb_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.glb"
            write_glb(path, {"asset": {"version": "2.0"}})
            handler.validate_input_glb(path)

    def test_output_validator_accepts_required_contract(self) -> None:
        document = {
            "asset": {"version": "2.0"},
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                            "indices": 3,
                            "material": 0,
                            "mode": 4,
                        }
                    ]
                }
            ],
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
            "textures": [{"source": 0}],
            "images": [{"bufferView": 0, "mimeType": "image/png"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.glb"
            write_glb(path, document)
            handler.validate_output_glb(path)

    def test_output_validator_rejects_missing_texture(self) -> None:
        document = {
            "asset": {"version": "2.0"},
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                            "indices": 3,
                            "material": 0,
                        }
                    ]
                }
            ],
            "materials": [{}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.glb"
            write_glb(path, document)
            with self.assertRaisesRegex(handler.ModelConvertError, "baseColorTexture"):
                handler.validate_output_glb(path)


if __name__ == "__main__":
    unittest.main()
