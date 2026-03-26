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
        Attempts to fix common AI JSON issues like unescaped quotes in long text blocks.
        """
        if not json_str:
            return ""

        # 1. Handle unescaped double quotes inside values
        # This is a heuristic: it looks for quotes that are NOT followed by , or } or ] or :
        # and NOT preceded by : or [ or {
        # Actually, a better way is to use a state machine, but let's try a robust regex first.
        
        # 2. Remove trailing commas
        json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
        
        # 3. Handle illegal control characters
        json_str = json_str.replace('\t', '    ')
        # We don't want to replace \n because it might be a valid newline in a string (which is still illegal JSON but common)
        # but let's at least ensure they are escaped if they are raw.
        
        return json_str.strip()

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
            # FALLBACK: If JSON fails, still try to extract basic metadata from the text report
            fallback_data = {
                "deal_model_data": {"title": "Direct Inference (Parsing Error)"},
                "metadata": {"ambiguous_points": ["AI response was truncated or malformed."]},
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
