# test_extract.py — collector/ 폴더에 저장
import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)

import anthropic
from config import config

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

message = client.messages.create(
    model=config.CLAUDE_MODEL,
    max_tokens=1024,
    system="You are a K-beauty consumer insights analyst. Respond only in JSON format. No other text.",
    messages=[{
        "role": "user",
        "content": """Analyze these YouTube comments about anua toner.

=== Comments ===
[150 likes] Does this work for oily skin?
[80 likes] I have sensitive skin, will this cause breakouts?
[60 likes] Is there white cast on dark skin?

=== Response Format (JSON) ===
{
  "consumer_problems": ["problem1"],
  "consumer_expectations": ["expectation1"],
  "skin_types_mentioned": ["oily"],
  "key_questions": ["question1"],
  "signal_strength": 0.8
}"""
    }],
)

print("=== 원본 응답 ===")
print(repr(message.content[0].text))
print("=== 파싱 시도 ===")
print(json.loads(message.content[0].text.strip()))