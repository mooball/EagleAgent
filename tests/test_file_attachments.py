import pytest
import io
import os
from PIL import Image
from unittest.mock import patch, MagicMock

from includes.chat.document_processing import process_image


class TestDocumentProcessing:
    def test_process_image_jpeg(self):
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        result = process_image(img_bytes.getvalue(), "image/jpeg")
        assert result["type"] == "image"
