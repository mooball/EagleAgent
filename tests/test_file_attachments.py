import pytest
import io
import os
from PIL import Image
from unittest.mock import patch, MagicMock

from includes.local_storage_client import LocalStorageClient
from includes.document_processing import process_image

class TestStorageIntegration:
    def test_local_storage_client_init(self, temp_storage_dir):
        client = LocalStorageClient(base_dir=temp_storage_dir)
        assert client.base_dir == temp_storage_dir

    @pytest.mark.asyncio
    async def test_local_storage_client_upload_and_read(self, temp_storage_dir):
        client = LocalStorageClient(base_dir=temp_storage_dir)
        file_path = os.path.join(temp_storage_dir, "test_file.txt")
        file_content = b"hello world"
            
        result = await client.upload_file("user/test_file.txt", file_content)
        assert result["object_key"] == "user/test_file.txt"
        assert result["url"] == "/files/user/test_file.txt"
        
        # Verify file actually wrote
        assert os.path.exists(os.path.join(temp_storage_dir, "user/test_file.txt"))

class TestDocumentProcessing:
    def test_process_image_jpeg(self):
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        result = process_image(img_bytes.getvalue(), "image/jpeg")
        assert result["type"] == "image"
