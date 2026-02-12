
import json
import re

def _strip_code_fences_original(text: str) -> str:
    """Original implementation from gemini_service.py"""
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        return "\n".join(lines)
    return text

def _extract_json_block(text: str) -> str:
    """
    Robustly extract JSON block from text using regex.
    Finds the first '[' or '{' and the last ']' or '}'.
    """
    text = text.strip()
    
    # Try to find a JSON array first (since we expect a list of Cortes)
    array_match = re.search(r'\[.*\]', text, re.DOTALL)
    if array_match:
        return array_match.group(0)
    
    # Fallback to finding a JSON object
    object_match = re.search(r'\{.*\}', text, re.DOTALL)
    if object_match:
        return object_match.group(0)
        
    # Fallback to original strip if regex fails (though unlikely for valid JSON)
    return _strip_code_fences_original(text)

def test_parsing(name, input_text):
    print(f"--- Test: {name} ---")
    print(f"Input: {input_text[:50]}...")
    
    try:
        # Original logic
        cleaned_original = _strip_code_fences_original(input_text)
        json.loads(cleaned_original)
        print("Original: SUCCESS")
    except json.JSONDecodeError as e:
        print(f"Original: FAILED ({e})")

    try:
        # New logic
        cleaned_new = _extract_json_block(input_text)
        json.loads(cleaned_new)
        print("New: SUCCESS")
        # print(f"Cleaned New: {cleaned_new[:50]}...")
    except json.JSONDecodeError as e:
        print(f"New: FAILED ({e})")
        # print(f"Cleaned New (Failed): {cleaned_new}")
    print()

if __name__ == "__main__":
    # Case 1: Standard clean JSON
    json1 = '[{"id": 1, "name": "test"}]'
    test_parsing("Clean JSON", json1)

    # Case 2: Markdown code block (fenced)
    json2 = '```json\n[{"id": 1, "name": "test"}]\n```'
    test_parsing("Markdown Block", json2)

    # Case 3: Text before and after
    json3 = 'Here is the JSON:\n```json\n[{"id": 1, "name": "test"}]\n```\nHope this helps.'
    test_parsing("Text with Markdown", json3)

    # Case 4: Text without code fences (common LLM failure)
    json4 = 'Here is the JSON you requested:\n[{"id": 1, "name": "test"}]'
    test_parsing("Text without Fences", json4)
    
    # Case 5: Trailing Comma (JSONDecodeError) - invalid JSON
    # This won't be fixed by extraction, but good to know
    json5 = '[{"id": 1, "name": "test",}]' 
    test_parsing("Trailing Comma", json5)
