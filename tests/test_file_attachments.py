"""
Tests for file attachment processing functionality.

Tests storage utilities and document processing without requiring actual GCS bucket.
"""

import pytest
import io
import base64
from PIL import Image
from unittest.mock import Mock, patch, MagicMock
from includes.document_processing import (
    process_image,
    extract_pdf_text,
    extract_text_from_file,
    process_file,
    create_multimodal_content
)
from includes.storage_utils import generate_object_key


class TestDocumentProcessing:
    """Test document processing functions."""
    
    def test_process_image_jpeg(self):
        """Test processing a JPEG image."""
        # Create a simple test image
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes = img_bytes.getvalue()
        
        result = process_image(img_bytes, "image/jpeg")
        
        assert result["type"] == "image"
        assert result["mime_type"] == "image/jpeg"
        assert result["size"] == (100, 100)
        assert result["format"] == "JPEG"
        assert len(result["base64"]) > 0
        
        # Verify base64 can be decoded
        decoded = base64.b64decode(result["base64"])
        assert len(decoded) > 0
    
    def test_process_image_png(self):
        """Test processing a PNG image."""
        img = Image.new('RGB', (50, 50), color='blue')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes = img_bytes.getvalue()
        
        result = process_image(img_bytes, "image/png")
        
        assert result["type"] == "image"
        assert result["mime_type"] == "image/png"
        assert result["size"] == (50, 50)
        assert "base64" in result
    
    def test_extract_text_from_text_file(self):
        """Test extracting text from a text file."""
        content = "Hello, this is a test file!\nLine 2\nLine 3"
        file_bytes = content.encode("utf-8")
        
        result = extract_text_from_file(file_bytes, "text/plain", "test.txt")
        
        assert result == content
        assert "Hello" in result
        assert "Line 2" in result
    
    def test_extract_text_utf8_encoding(self):
        """Test text extraction with UTF-8 encoding."""
        content = "Hello 世界 🌍"
        file_bytes = content.encode("utf-8")
        
        result = extract_text_from_file(file_bytes, "text/plain", "unicode.txt")
        
        assert result == content
    
    def test_process_file_image(self):
        """Test process_file with an image."""
        img = Image.new('RGB', (100, 100), color='green')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes = img_bytes.getvalue()
        
        result = process_file(img_bytes, "image/jpeg", "test.jpg")
        
        assert result["filename"] == "test.jpg"
        assert result["mime_type"] == "image/jpeg"
        assert result["processed_type"] == "image"
        assert result["size_bytes"] == len(img_bytes)
        assert "content" in result
        assert result["content"]["type"] == "image"
    
    def test_process_file_text(self):
        """Test process_file with a text file."""
        content = "Sample text content"
        file_bytes = content.encode("utf-8")
        
        result = process_file(file_bytes, "text/plain", "sample.txt")
        
        assert result["filename"] == "sample.txt"
        assert result["mime_type"] == "text/plain"
        assert result["processed_type"] == "text"
        assert result["content"] == content
    
    def test_create_multimodal_content_text_only(self):
        """Test creating multimodal content with text only."""
        text = "Hello, world!"
        processed_files = []
        
        content = create_multimodal_content(text, processed_files)
        
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Hello, world!"
    
    def test_create_multimodal_content_with_image(self):
        """Test creating multimodal content with text and image."""
        text = "What's in this image?"
        
        # Create test image
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes = img_bytes.getvalue()
        
        processed_file = process_file(img_bytes, "image/jpeg", "test.jpg")
        
        content = create_multimodal_content(text, [processed_file])
        
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "What's in this image?"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    
    def test_create_multimodal_content_with_text_file(self):
        """Test creating multimodal content with uploaded text file."""
        text = "Analyze this document:"
        
        doc_content = "Document line 1\nDocument line 2"
        file_bytes = doc_content.encode("utf-8")
        
        processed_file = process_file(file_bytes, "text/plain", "document.txt")
        
        content = create_multimodal_content(text, [processed_file])
        
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "Analyze this document:" in content[0]["text"]
        assert "[Content from document.txt]:" in content[0]["text"]
        assert "Document line 1" in content[0]["text"]
    
    def test_create_multimodal_content_multiple_files(self):
        """Test creating multimodal content with multiple files."""
        text = "Analyze these files"
        
        # Create an image
        img = Image.new('RGB', (50, 50), color='blue')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes = img_bytes.getvalue()
        image_file = process_file(img_bytes, "image/png", "image.png")
        
        # Create a text file
        text_content = "Text file content"
        text_bytes = text_content.encode("utf-8")
        text_file = process_file(text_bytes, "text/plain", "notes.txt")
        
        content = create_multimodal_content(text, [image_file, text_file])
        
        # Should have text part (with embedded text file) and image part
        assert len(content) == 2
        text_part = content[0]
        assert text_part["type"] == "text"
        assert "Analyze these files" in text_part["text"]
        assert "[Content from notes.txt]:" in text_part["text"]
        
        image_part = content[1]
        assert image_part["type"] == "image_url"
        assert image_part["image_url"].startswith("data:image/png;base64,")


class TestStorageUtils:
    """Test storage utility functions."""
    
    def test_generate_object_key(self):
        """Test generating GCS object keys."""
        user_id = "user@example.com"
        thread_id = "thread-123"
        filename = "test file.pdf"
        
        object_key = generate_object_key(user_id, thread_id, filename)
        
        assert object_key.startswith("uploads/user@example.com/thread-123/")
        assert "test_file.pdf" in object_key  # Spaces replaced with underscores
        assert len(object_key.split("/")) == 4  # uploads/user/thread/file
    
    def test_generate_object_key_with_spaces(self):
        """Test that spaces in filenames are replaced."""
        object_key = generate_object_key("user", "thread", "my document.pdf")
        
        assert " " not in object_key
        assert "my_document.pdf" in object_key
    
    @patch('includes.storage_utils.get_storage_client')
    def test_upload_file_to_gcs_mock(self, mock_get_client):
        """Test GCS upload with mocked client."""
        from includes.storage_utils import upload_file_to_gcs
        
        # Mock the storage client
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.public_url = "https://storage.googleapis.com/bucket/file.txt"
        
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_get_client.return_value = mock_client
        
        # Create temporary test file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write("test content")
            temp_path = f.name
        
        try:
            url = upload_file_to_gcs(temp_path, "test-bucket", "uploads/test.txt")
            
            assert url == "https://storage.googleapis.com/bucket/file.txt"
            mock_client.bucket.assert_called_once_with("test-bucket")
            mock_bucket.blob.assert_called_once_with("uploads/test.txt")
            mock_blob.upload_from_filename.assert_called_once()
        finally:
            import os
            os.unlink(temp_path)


@pytest.mark.integration
class TestFileAttachmentIntegration:
    """Integration tests for file attachment workflow."""
    
    def test_end_to_end_image_processing(self):
        """Test complete workflow for image processing."""
        # Create test image
        img = Image.new('RGB', (200, 200), color='yellow')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes = img_bytes.getvalue()
        
        # Process file
        processed = process_file(img_bytes, "image/jpeg", "test.jpg")
        
        # Create multimodal content
        content = create_multimodal_content("Describe this image", [processed])
        
        # Verify structure matches what LangChain expects
        assert isinstance(content, list)
        assert len(content) == 2
        
        # Text part
        assert content[0]["type"] == "text"
        assert isinstance(content[0]["text"], str)
        
        # Image part
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
        
        # Verify base64 data is valid
        base64_data = content[1]["image_url"].split(",")[1]
        decoded = base64.b64decode(base64_data)
        assert len(decoded) > 0
    
    def test_end_to_end_text_processing(self):
        """Test complete workflow for text file processing."""
        text_content = "Important document content\nLine 2\nLine 3"
        file_bytes = text_content.encode("utf-8")
        
        # Process file
        processed = process_file(file_bytes, "text/plain", "document.txt")
        
        # Create multimodal content
        content = create_multimodal_content("Summarize this", [processed])
        
        # Verify structure
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "Summarize this" in content[0]["text"]
        assert "[Content from document.txt]:" in content[0]["text"]
        assert "Important document content" in content[0]["text"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
