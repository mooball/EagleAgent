"""
Document processing utilities for extracting content from various file types.

Supports:
- Images: Base64 encoding for vision models
- PDFs: Text extraction
- Text files: Direct reading
- Audio: Placeholder for future transcription
"""

import base64
import io
import logging
from typing import Dict, Any, Optional
from PIL import Image
import pdfplumber

logger = logging.getLogger(__name__)


def process_image(file_bytes: bytes, mime_type: str) -> Dict[str, Any]:
    """
    Process an image file for use with vision models.
    
    Converts image to base64 encoding and validates format.
    
    Args:
        file_bytes: Image file content as bytes
        mime_type: MIME type of the image (e.g., "image/jpeg")
        
    Returns:
        dict: Processed image data with base64 encoding and metadata
            {
                "type": "image",
                "base64": str,
                "mime_type": str,
                "size": tuple (width, height),
                "format": str
            }
    """
    try:
        # Load and validate image
        image = Image.open(io.BytesIO(file_bytes))
        
        # Get image info
        width, height = image.size
        format_name = image.format
        
        # Convert to RGB if necessary (for PNG with transparency, etc.)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        
        # Encode to base64
        buffered = io.BytesIO()
        # Use original format if available, otherwise JPEG
        save_format = format_name if format_name else "JPEG"
        image.save(buffered, format=save_format)
        img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        logger.info(f"Processed image: {format_name}, {width}x{height}")
        
        return {
            "type": "image",
            "base64": img_base64,
            "mime_type": mime_type,
            "size": (width, height),
            "format": format_name or "JPEG"
        }
        
    except Exception as e:
        logger.error(f"Failed to process image: {e}")
        raise


def extract_pdf_text(file_bytes: bytes) -> str:
    """
    Extract text content from a PDF file.
    
    Args:
        file_bytes: PDF file content as bytes
        
    Returns:
        str: Extracted text from all pages
    """
    try:
        text_content = []
        
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                page_text = page.extract_text()
                if page_text:
                    text_content.append(f"--- Page {page_num} ---\n{page_text}")
        
        full_text = "\n\n".join(text_content)
        
        logger.info(f"Extracted text from PDF: {len(full_text)} characters, {len(text_content)} pages")
        
        return full_text if full_text else "[PDF contains no extractable text]"
        
    except Exception as e:
        logger.error(f"Failed to extract PDF text: {e}")
        return f"[Error extracting PDF text: {str(e)}]"


def extract_text_from_file(file_bytes: bytes, mime_type: str, filename: str = "") -> str:
    """
    Extract text content from various file types.
    
    Args:
        file_bytes: File content as bytes
        mime_type: MIME type of the file
        filename: Original filename (optional, for extension-based detection)
        
    Returns:
        str: Extracted text content
    """
    try:
        # Handle PDFs
        if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            return extract_pdf_text(file_bytes)
        
        # Handle text files
        if mime_type.startswith("text/") or filename.lower().endswith((".txt", ".md", ".json", ".xml", ".csv", ".log")):
            # Try different encodings
            for encoding in ["utf-8", "latin-1", "cp1252"]:
                try:
                    text = file_bytes.decode(encoding)
                    logger.info(f"Decoded text file using {encoding}: {len(text)} characters")
                    return text
                except UnicodeDecodeError:
                    continue
            
            return "[Unable to decode text file with supported encodings]"
        
        # Unsupported file type for text extraction
        return f"[Text extraction not supported for {mime_type}]"
        
    except Exception as e:
        logger.error(f"Failed to extract text from file: {e}")
        return f"[Error extracting text: {str(e)}]"


def process_audio(file_bytes: bytes, mime_type: str) -> Dict[str, Any]:
    """
    Process an audio file (placeholder for future transcription support).
    
    Args:
        file_bytes: Audio file content as bytes
        mime_type: MIME type of the audio file
        
    Returns:
        dict: Audio metadata (transcription to be implemented)
            {
                "type": "audio",
                "mime_type": str,
                "size_bytes": int,
                "transcription": str (placeholder)
            }
    """
    logger.info(f"Audio file received: {mime_type}, {len(file_bytes)} bytes")
    
    # Placeholder - transcription can be added later using Whisper API or similar
    return {
        "type": "audio",
        "mime_type": mime_type,
        "size_bytes": len(file_bytes),
        "transcription": "[Audio transcription not yet implemented]"
    }


def process_file(
    file_bytes: bytes, 
    mime_type: str, 
    filename: str
) -> Dict[str, Any]:
    """
    Process any supported file type and extract relevant content.
    
    Main entry point for file processing. Routes to appropriate handler
    based on MIME type.
    
    Args:
        file_bytes: File content as bytes
        mime_type: MIME type of the file
        filename: Original filename
        
    Returns:
        dict: Processed file data including extracted content
            Structure varies by file type but always includes:
            {
                "filename": str,
                "mime_type": str,
                "size_bytes": int,
                "processed_type": str,  # "image", "text", "pdf", "audio"
                "content": str or dict  # Extracted content
            }
    """
    result = {
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": len(file_bytes)
    }
    
    try:
        # Process images
        if mime_type.startswith("image/"):
            image_data = process_image(file_bytes, mime_type)
            result["processed_type"] = "image"
            result["content"] = image_data
            
        # Process PDFs
        elif mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            text = extract_pdf_text(file_bytes)
            result["processed_type"] = "pdf"
            result["content"] = text
            
        # Process text files
        elif mime_type.startswith("text/"):
            text = extract_text_from_file(file_bytes, mime_type, filename)
            result["processed_type"] = "text"
            result["content"] = text
            
        # Process audio files
        elif mime_type.startswith("audio/"):
            audio_data = process_audio(file_bytes, mime_type)
            result["processed_type"] = "audio"
            result["content"] = audio_data
            
        else:
            logger.warning(f"Unsupported file type: {mime_type}")
            result["processed_type"] = "unsupported"
            result["content"] = f"[File type {mime_type} not yet supported for processing]"
        
        logger.info(f"Successfully processed file: {filename} ({result['processed_type']})")
        return result
        
    except Exception as e:
        logger.error(f"Error processing file {filename}: {e}")
        result["processed_type"] = "error"
        result["content"] = f"[Error processing file: {str(e)}]"
        return result


def create_multimodal_content(text: str, processed_files: list[Dict[str, Any]]) -> list:
    """
    Create multimodal content for LangChain messages.
    
    Combines text with processed files (especially images) into the format
    expected by multimodal LLMs like Gemini.
    
    Args:
        text: User's text message
        processed_files: List of processed file data from process_file()
        
    Returns:
        list: Content parts for HumanMessage, e.g.:
            [
                {"type": "text", "text": "..."},
                {"type": "image_url", "image_url": "data:image/jpeg;base64,..."}
            ]
    """
    content_parts = []
    
    # Add text content first
    if text:
        content_parts.append({
            "type": "text",
            "text": text
        })
    
    # Add file contents
    for file_data in processed_files:
        processed_type = file_data.get("processed_type")
        
        if processed_type == "image":
            # Add image as base64 data URL
            image_content = file_data["content"]
            mime_type = image_content["mime_type"]
            base64_data = image_content["base64"]
            
            content_parts.append({
                "type": "image_url",
                "image_url": f"data:{mime_type};base64,{base64_data}"
            })
            
        elif processed_type in ("pdf", "text"):
            # Add extracted text to the text content
            extracted_text = file_data["content"]
            filename = file_data["filename"]
            
            # Append as part of the text message
            if content_parts and content_parts[0]["type"] == "text":
                content_parts[0]["text"] += f"\n\n[Content from {filename}]:\n{extracted_text}"
            else:
                content_parts.insert(0, {
                    "type": "text",
                    "text": f"[Content from {filename}]:\n{extracted_text}"
                })
                
        elif processed_type == "audio":
            # For now, just mention the audio file
            filename = file_data["filename"]
            if content_parts and content_parts[0]["type"] == "text":
                content_parts[0]["text"] += f"\n\n[Audio file attached: {filename}]"
            else:
                content_parts.insert(0, {
                    "type": "text",
                    "text": f"[Audio file attached: {filename}]"
                })
    
    return content_parts if content_parts else [{"type": "text", "text": text or ""}]
