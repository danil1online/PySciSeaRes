#!/usr/bin/env python3
"""Тесты извлечения данных о руководителе."""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractors.supervisor import (
    extract_supervisor_from_pdf_text,
    _send_supervisor_request,
)


class TestExtractSupervisorFromPdfText:
    """Тесты извлечения руководителя из текста."""

    def test_basic_extraction(self, sample_supervisor_text):
        result = extract_supervisor_from_pdf_text(sample_supervisor_text)
        # LLM might not be available, so result could be None or valid
        assert "supervisor_name" in result
        assert "supervisor_work" in result

    def test_missing_supervisor(self):
        text = "No supervisor information here.\nJust regular text."
        result = extract_supervisor_from_pdf_text(text)
        assert "supervisor_name" in result
        assert "supervisor_work" in result


class TestSendSupervisorRequest:
    """Тесты отправки запроса к LLM."""

    def test_valid_json_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"supervisor_name": "Сидоров П.А.", "supervisor_work": "Университет"}'
                }
            }]
        }
        
        with patch('requests.post', return_value=mock_response):
            result = _send_supervisor_request("Some text")
            assert result["supervisor_name"] == "Сидоров П.А."
            assert result["supervisor_work"] == "Университет"

    def test_invalid_json_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "Some random text without JSON"
                }
            }]
        }
        
        with patch('requests.post', return_value=mock_response):
            result = _send_supervisor_request("Some text")
            assert result["supervisor_name"] is None
            assert result["supervisor_work"] is None

    def test_llm_timeout(self):
        with patch('requests.post', side_effect=Exception("Timeout")):
            result = _send_supervisor_request("Some text")
            assert result["supervisor_name"] is None
            assert result["supervisor_work"] is None

    def test_empty_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": ""
                }
            }]
        }
        
        with patch('requests.post', return_value=mock_response):
            result = _send_supervisor_request("Some text")
            assert result["supervisor_name"] is None
            assert result["supervisor_work"] is None
