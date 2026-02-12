
import sys
import json
import logging
from unittest.mock import MagicMock

# Mock settings and dependencies
sys.modules["core.config"] = MagicMock()
sys.modules["google.genai"] = MagicMock()
sys.modules["google"] = MagicMock()

# Import the service function to test
# We need to bypass the top-level imports that might fail due to missing env vars or deps
# So we will mock them in sys.modules BEFORE importing
sys.path.append("/Users/fernando/Documents/api-cortes")
from services.gemini_service import _extract_json_payload

def test_extraction():
    test_cases = [
        (
            "Clean JSON",
            '[{"id": 1}]',
            '[{"id": 1}]'
        ),
        (
            "Markdown Block",
            '```json\n[{"id": 1}]\n```',
            '[{"id": 1}]'
        ),
        (
            "Text wrapping",
            'Here is the JSON:\n[{"id": 1}]\nThanks.',
            '[{"id": 1}]'
        ),
        (
            "Text wrapping with markdown",
            'Here is the JSON:\n```json\n[{"id": 1}]\n```\nThanks.',
            '[{"id": 1}]'
        ),
         (
            "Nested Brackets (should take outermost)",
            'Wrapper [ {"inside": [1, 2]} ] End',
            '[ {"inside": [1, 2]} ]'
        )
    ]

    print("Running JSON Extraction Tests...\n")
    failed = False
    for name, input_text, expected in test_cases:
        result = _extract_json_payload(input_text)
        # Normalize whitespace for comparison
        result_clean = "".join(result.split())
        expected_clean = "".join(expected.split())
        
        if result_clean == expected_clean:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}")
            print(f"  Input: {input_text!r}")
            print(f"  Expected: {expected!r}")
            print(f"  Got: {result!r}")
            failed = True
    
    if failed:
        sys.exit(1)
    else:
        print("\nAll tests passed!")

if __name__ == "__main__":
    test_extraction()
