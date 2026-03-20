import requests
import json
import logging
from typing import Iterator, Dict, Any, Generator
from django.conf import settings

logger = logging.getLogger(__name__)

class OllamaProviderService:
    """
    Handles pure HTTP communication with the Ollama backend.
    Responsible for executing requests, handling retries, and streaming low-level byte buffers.
    """
    def __init__(self):
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')

    def get_available_models(self) -> list[str]:
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model['name'] for model in data.get('models', [])]
        except Exception as e:
            logger.error(f"Error fetching Ollama models: {str(e)}")
            return []

    def execute_stream(self, payload: dict) -> Iterator[str]:
        """
        Executes a streaming request and yields decoded JSON lines.
        """
        response = requests.post(f"{self.ollama_url}/api/generate", json=payload, stream=True, timeout=300)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if line:
                yield line.decode('utf-8')

    def execute_standard(self, payload: dict, timeout: int = 2400) -> dict:
        """
        Executes a standard synchronous request and returns the parsed JSON response.
        """
        response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
