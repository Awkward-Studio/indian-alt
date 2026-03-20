import json
import re
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ResponseParserService:
    """
    Extracts `<thinking>`/`<response>` blocks and robustly parses embedded JSON
    out of raw LLM markdown outputs.
    """

    @staticmethod
    def extract_json(text: str) -> str:
        """Robustly find and extract JSON string from a larger text block."""
        try:
            # 1. Try common tags/blocks first
            patterns = [
                r'<json>\s*(\{.*?\})\s*</json>',
                r'```json\s*(\{.*?\})\s*```',
                r'```\s*(\{.*?\})\s*```'
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    return match.group(1).strip()

            # 2. Fallback to raw brace matching
            first_brace = text.find('{')
            last_brace = text.rfind('}')
            if first_brace != -1 and last_brace != -1:
                return text[first_brace:last_brace + 1]
        except Exception as e:
            logger.error(f"JSON extraction failed: {str(e)}")
        return text

    @staticmethod
    def parse_standard_response(raw_response: str, thinking_text: str, is_extraction_skill: bool = False) -> Dict[str, Any]:
        """
        Cleans standard LLM response payload, splitting thinking from the final output, 
        and extracts JSON payloads.
        """
        thinking = thinking_text
        if not thinking and "<thinking>" in raw_response:
            t_match = re.search(r'<thinking>(.*?)</thinking>', raw_response, re.DOTALL)
            if t_match:
                thinking = t_match.group(1).strip()
                
        clean_response = raw_response
        if "<response>" in clean_response:
            r_match = re.search(r'<response>(.*?)</response>', clean_response, re.DOTALL)
            if r_match:
                clean_response = r_match.group(1).strip()
                
        clean_response = re.sub(r'<thinking>.*?</thinking>', '', clean_response, flags=re.DOTALL).strip()
        clean_response = clean_response.replace("<response>", "").replace("</response>", "").strip()

        clean_json_str = ResponseParserService.extract_json(clean_response)
        
        try:
            parsed_json = json.loads(clean_json_str)
            
            if is_extraction_skill:
                if "deal_model_data" not in parsed_json: parsed_json["deal_model_data"] = {}
                if "metadata" not in parsed_json: parsed_json["metadata"] = {"ambiguous_points": [], "missing_fields": []}
                if "analyst_report" not in parsed_json: parsed_json["analyst_report"] = raw_response
            
            parsed_json["thinking"] = thinking
            parsed_json["response"] = clean_response
            parsed_json["_raw_response"] = raw_response
            return parsed_json, True, clean_response, thinking
        except Exception as e:
            return {"error": "JSON parsing error", "raw": raw_response, "thinking": thinking, "err_msg": str(e)}, False, clean_response, thinking

    @staticmethod
    def parse_stream(stream_iterator):
        """
        Stateful streaming parser for <thinking> and <response> tags.
        Yields structured chunks for the frontend.
        """
        full_response = ""
        full_thinking = ""
        is_thinking_mode = False
        is_response_mode = False
        buffer = ""

        for line in stream_iterator:
            chunk = json.loads(line)
            raw_text = chunk.get("response") or ""
            buffer += raw_text
            
            while True:
                if is_thinking_mode:
                    if "</thinking>" in buffer:
                        parts = buffer.split("</thinking>", 1)
                        content = parts[0]
                        yield {"response": "", "thinking": content, "done": False}, content, ""
                        full_thinking += content
                        buffer = parts[1]
                        is_thinking_mode = False
                    else:
                        if "</" in buffer:
                            tag_start = buffer.find("</")
                            to_yield = buffer[:tag_start]
                            if to_yield:
                                yield {"response": "", "thinking": to_yield, "done": False}, to_yield, ""
                                full_thinking += to_yield
                                buffer = buffer[tag_start:]
                            break
                        else:
                            if buffer:
                                yield {"response": "", "thinking": buffer, "done": False}, buffer, ""
                                full_thinking += buffer
                                buffer = ""
                            break
                elif is_response_mode:
                    if "</response>" in buffer:
                        parts = buffer.split("</response>", 1)
                        content = parts[0]
                        yield {"response": content, "thinking": "", "done": False}, "", content
                        full_response += content
                        buffer = parts[1]
                        is_response_mode = False
                    else:
                        if "</" in buffer:
                            tag_start = buffer.find("</")
                            to_yield = buffer[:tag_start]
                            if to_yield:
                                yield {"response": to_yield, "thinking": "", "done": False}, "", to_yield
                                full_response += to_yield
                                buffer = buffer[tag_start:]
                            break
                        else:
                            if buffer:
                                yield {"response": buffer, "thinking": "", "done": False}, "", buffer
                                full_response += buffer
                                buffer = ""
                            break
                else:
                    if "<thinking>" in buffer:
                        parts = buffer.split("<thinking>", 1)
                        if parts[0]:
                            yield {"response": parts[0], "thinking": "", "done": False}, "", parts[0]
                            full_response += parts[0]
                        buffer = parts[1]
                        is_thinking_mode = True
                    elif "<response>" in buffer:
                        parts = buffer.split("<response>", 1)
                        if parts[0]:
                            yield {"response": parts[0], "thinking": "", "done": False}, "", parts[0]
                            full_response += parts[0]
                        buffer = parts[1]
                        is_response_mode = True
                    elif "<" in buffer:
                        tag_start = buffer.find("<")
                        to_yield = buffer[:tag_start]
                        if to_yield:
                            yield {"response": to_yield, "thinking": "", "done": False}, "", to_yield
                            full_response += to_yield
                            buffer = buffer[tag_start:]
                        break
                    else:
                        if buffer:
                            yield {"response": buffer, "thinking": "", "done": False}, "", buffer
                            full_response += buffer
                            buffer = ""
                        break

            if chunk.get("done"):
                if buffer:
                    yield {"response": buffer, "thinking": "", "done": False}, "", buffer
                break
