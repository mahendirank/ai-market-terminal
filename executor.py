import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(__file__))
from ppt_generator import create_ppt

BASE_PATH = os.path.expanduser(
    f"~/ai-system/outputs/project_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
)

os.makedirs(BASE_PATH, exist_ok=True)


def save_file(filename, content):
    path = os.path.join(BASE_PATH, filename)

    with open(path, "w") as f:
        f.write(content)

    print(f"\n✅ File saved: {path}\n")


def extract_code_blocks(text):
    import re
    pattern = r"```(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


def handle_output(user_input, ai_output):
    user_input = user_input.lower()

    code_blocks = extract_code_blocks(ai_output)

    # TradingView
    if "tradingview" in user_input or "pine" in user_input:
        if code_blocks:
            save_file("indicator.pine", code_blocks[0])

    # MT5
    elif "mt5" in user_input or "ea" in user_input:
        if code_blocks:
            save_file("expert_advisor.mq5", code_blocks[0])

    # PPT
    elif "ppt" in user_input:
        create_ppt(ai_output)

    # Default
    else:
        save_file("output.txt", ai_output)
