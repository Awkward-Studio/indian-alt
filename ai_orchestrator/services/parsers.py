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
    def repair_json(json_str: str) -> str:
        """
        Professional stack-based repair for truncated or malformed AI JSON.
        Handles unterminated strings, mismatched braces, and trailing commas.
        """
        if not json_str:
            return ""

        # 1. Basic cleanup
        json_str = json_str.strip()
        json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
        json_str = json_str.replace('\t', '    ')

        # 2. Stack-based repair
        stack = []
        in_string = False
        escaped = False
        
        for char in json_str:
            if escaped:
                escaped = False
                continue
            
            if char == '\\':
                escaped = True
                continue
                
            if char == '"':
                in_string = not in_string
                continue
                
            if not in_string:
                if char == '{':
                    stack.append('}')
                elif char == '[':
                    stack.append(']')
                elif char in ('}', ']'):
                    if stack and stack[-1] == char:
                        stack.pop()

        # 3. Apply repairs in correct order
        if in_string:
            json_str += '"'
            
        # Close remaining structures from the stack (in reverse order of opening)
        while stack:
            json_str += stack.pop()
            
        return json_str

    @staticmethod
    def extract_json(text: str) -> str:
        """Robustly find and extract JSON string from a larger text block."""
        try:
            # 1. Try to find content between tags (non-greedy to avoid capturing multiple blocks if tags are repeated)
            patterns = [
                r'<json>(.*?)</json>',
                r'```json(.*?)\s*```',
                r'```(.*?)\s*```'
            ]
            
            candidate = None
            for pattern in patterns:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    candidate = match.group(1).strip()
                    break
            
            if not candidate:
                # Fallback to finding from first {
                first_brace = text.find('{')
                if first_brace != -1:
                    candidate = text[first_brace:].strip()
            
            if not candidate:
                return text

            # 2. Use raw_decode to get the FIRST valid JSON object from the candidate
            # This handles cases where the AI appends extra text or repeats the JSON structure.
            start_idx = candidate.find('{')
            if start_idx == -1:
                return candidate
                
            # Attempt to repair common issues before raw_decode to increase success rate
            # (e.g., trailing commas or unescaped characters that raw_decode might stumble on)
            # but we only repair the first object part if possible.
            
            try:
                decoder = json.JSONDecoder()
                # raw_decode returns (object, end_index)
                obj, _ = decoder.raw_decode(candidate[start_idx:])
                return json.dumps(obj)
            except json.JSONDecodeError:
                # If raw_decode fails on the raw string, try repairing it and decoding again
                repaired_candidate = ResponseParserService.repair_json(candidate[start_idx:])
                try:
                    obj, _ = decoder.raw_decode(repaired_candidate)
                    return json.dumps(obj)
                except:
                    # Final fallback: return the original candidate if all decoding/repair fails
                    return candidate
        except Exception as e:
            logger.error(f"JSON extraction failed: {str(e)}")
        return text

    @staticmethod
    def _find_key_position(text: str, key: str) -> int:
        pattern = re.compile(rf'"{re.escape(key)}"\s*:')
        match = pattern.search(text)
        return match.end() if match else -1

    @staticmethod
    def _extract_json_value_fragment(text: str, key: str) -> str | None:
        value_start = ResponseParserService._find_key_position(text, key)
        if value_start == -1:
            return None

        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text):
            return None

        opening = text[value_start]
        if opening in "{[":
            closing = "}" if opening == "{" else "]"
            depth = 0
            in_string = False
            escape = False
            for index in range(value_start, len(text)):
                char = text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        return text[value_start:index + 1]
            return None

        if opening == '"':
            escape = False
            for index in range(value_start + 1, len(text)):
                char = text[index]
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    return text[value_start:index + 1]
            return None

        scalar_match = re.match(r'-?\d+(?:\.\d+)?|true|false|null', text[value_start:])
        if scalar_match:
            return scalar_match.group(0)
        return None

    @staticmethod
    def _load_value_fragment(fragment: str | None):
        if not fragment:
            return None
        try:
            return json.loads(fragment)
        except Exception:
            repaired = ResponseParserService.repair_json(fragment)
            try:
                return json.loads(repaired)
            except Exception:
                return None

    @staticmethod
    def _coerce_string(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        if isinstance(value, (int, float)):
            return str(value)
        return None

    @staticmethod
    def _coerce_string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def salvage_extraction_payload(text: str, clean_response: str = "", thinking: str = "") -> Dict[str, Any] | None:
        candidate = ResponseParserService.extract_json(text)

        model_data = ResponseParserService._load_value_fragment(
            ResponseParserService._extract_json_value_fragment(candidate, "deal_model_data")
        )
        metadata = ResponseParserService._load_value_fragment(
            ResponseParserService._extract_json_value_fragment(candidate, "metadata")
        )
        analyst_report = ResponseParserService._load_value_fragment(
            ResponseParserService._extract_json_value_fragment(candidate, "analyst_report")
        )

        if not isinstance(model_data, dict) and not isinstance(metadata, dict):
            return None

        normalized_model_data = model_data if isinstance(model_data, dict) else {}
        normalized_metadata = metadata if isinstance(metadata, dict) else {}

        normalized_model_data = {
            "title": ResponseParserService._coerce_string(normalized_model_data.get("title")),
            "industry": ResponseParserService._coerce_string(normalized_model_data.get("industry")),
            "sector": ResponseParserService._coerce_string(normalized_model_data.get("sector")),
            "funding_ask": ResponseParserService._coerce_string(normalized_model_data.get("funding_ask")),
            "funding_ask_for": ResponseParserService._coerce_string(normalized_model_data.get("funding_ask_for")),
            "priority": ResponseParserService._coerce_string(normalized_model_data.get("priority")),
            "city": ResponseParserService._coerce_string(normalized_model_data.get("city")),
            "state": ResponseParserService._coerce_string(normalized_model_data.get("state")),
            "country": ResponseParserService._coerce_string(normalized_model_data.get("country")),
            "themes": ResponseParserService._coerce_string_list(normalized_model_data.get("themes")),
        }
        normalized_model_data = {
            key: value for key, value in normalized_model_data.items()
            if value not in (None, [], "")
        }

        normalized_metadata = {
            "ambiguous_points": ResponseParserService._coerce_string_list(normalized_metadata.get("ambiguous_points")),
            "sources_cited": ResponseParserService._coerce_string_list(normalized_metadata.get("sources_cited")),
            "parse_mode": "salvaged",
            "parse_warning": "Structured fields were salvaged from a malformed or truncated AI response.",
        }

        if not isinstance(analyst_report, str) or not analyst_report.strip():
            analyst_report = clean_response.strip() or text.strip()

        return {
            "deal_model_data": normalized_model_data,
            "metadata": normalized_metadata,
            "analyst_report": analyst_report,
            "thinking": thinking,
            "response": clean_response,
            "_raw_response": text,
            "_salvaged": True,
            "error": "JSON parsing error: structured fields salvaged from malformed AI output.",
        }

    @staticmethod
    def parse_standard_response(raw_response: str, thinking_text: str, is_extraction_skill: bool = False) -> Dict[str, Any]:
        """
        Cleans standard LLM response payload, splitting thinking from the final output, 
        and extracts JSON payloads.
        """
        thinking = thinking_text
        if not thinking and ("<thinking>" in raw_response or "<think>" in raw_response):
            t_match = re.search(r'<thinking>(.*?)</thinking>', raw_response, re.DOTALL)
            if not t_match:
                t_match = re.search(r'<think>(.*?)</think>', raw_response, re.DOTALL)
            if t_match:
                thinking = t_match.group(1).strip()

        # 1. EXTRACT JSON FIRST BEFORE STRIPPING ANYTHING ELSE
        # extract_json now robustly finds the first object and attempts repairs
        clean_json_str = ResponseParserService.extract_json(raw_response)
                
        # 2. CLEAN RESPONSE FOR DISPLAY
        clean_response = raw_response
        if "<response>" in clean_response:
            r_match = re.search(r'<response>(.*?)</response>', clean_response, re.DOTALL)
            if r_match:
                clean_response = r_match.group(1).strip()
                
        clean_response = re.sub(r'<thinking>.*?</thinking>', '', clean_response, flags=re.DOTALL).strip()
        clean_response = re.sub(r'<think>.*?</think>', '', clean_response, flags=re.DOTALL).strip()
        clean_response = clean_response.replace("<response>", "").replace("</response>", "").strip()
        # Also strip the <json> block from the display text if it's there
        clean_response = re.sub(r'<json>.*?</json>', '', clean_response, flags=re.DOTALL).strip()

        # 3. PARSE JSON
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
            logger.error(f"JSON parsing/repair failed: {str(e)}")
            if is_extraction_skill:
                salvaged = ResponseParserService.salvage_extraction_payload(raw_response, clean_response, thinking)
                if salvaged:
                    return salvaged, False, clean_response, thinking
            # FALLBACK: If JSON fails, still try to extract basic metadata from the text report
            fallback_data = {
                "deal_model_data": {"title": "Direct Inference (Parsing Error)"},
                "metadata": {
                    "ambiguous_points": ["AI response was truncated or malformed."],
                    "parse_mode": "failed",
                    "parse_warning": "No structured fields could be salvaged from the malformed AI response.",
                },
                "analyst_report": raw_response,
                "error": f"JSON parsing error: {str(e)}",
                "thinking": thinking,
                "response": clean_response
            }
            
            # Simple heuristic: look for "Investment Analysis: Company Name" or similar in the first 5 lines
            first_lines = raw_response.split('\n')[:10]
            for line in first_lines:
                if line.startswith('# ') or line.startswith('## '):
                    title = line.replace('#', '').replace('Investment Analysis:', '').replace('Report', '').strip()
                    if title:
                        fallback_data["deal_model_data"]["title"] = title
                        break
            
            return fallback_data, False, clean_response, thinking

    @staticmethod
    def parse_stream(stream_iterator):
        """
        Stateful streaming parser for <thinking>/<think> and <response> tags.
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
            raw_thinking = chunk.get("thinking") or ""
            if raw_thinking:
                yield {"response": "", "thinking": raw_thinking, "done": False}, raw_thinking, ""
            buffer += raw_text
            
            while True:
                if is_thinking_mode:
                    closing_tag = ResponseParserService._first_tag(buffer, ("</thinking>", "</think>"))
                    if closing_tag:
                        parts = buffer.split(closing_tag, 1)
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
                    thinking_tag = ResponseParserService._first_tag(buffer, ("<thinking>", "<think>"))
                    if thinking_tag:
                        parts = buffer.split(thinking_tag, 1)
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

    @staticmethod
    def _first_tag(text: str, tags: tuple[str, ...]) -> str | None:
        positions = [
            (text.find(tag), tag)
            for tag in tags
            if text.find(tag) != -1
        ]
        if not positions:
            return None
        return min(positions, key=lambda item: item[0])[1]
