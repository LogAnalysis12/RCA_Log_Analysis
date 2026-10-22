from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traversal.llm_backends import get_backend


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test Amazon Bedrock Converse connectivity."
    )
    parser.add_argument(
        "--config",
        default="./config.json",
    )
    args = parser.parse_args()

    try:
        config_path = Path(args.config).resolve()

        config = json.loads(
            config_path.read_text(
                encoding="utf-8-sig",
            )
        )

        llm_config = config["llm"]

        print("Testing Amazon Bedrock...")
        print("Provider:", llm_config["provider"])
        print("Region:", llm_config["region"])
        print("Model ID configured:", bool(llm_config["model"]))

        backend = get_backend(
            canonical_tools=[],
            system_prompt=(
                "You are a connectivity-test assistant. "
                "Reply with exactly: BEDROCK_CONNECTION_OK"
            ),
            config=llm_config,
        )

        result = backend.send(
            "Return the required connectivity-test response."
        )

        print("")
        print("========== BEDROCK CONNECTIVITY TEST ==========")
        print("Response:", result.text.strip())
        print("Stop reason:", result.stop_reason)
        print("Input tokens:", result.usage["input_tokens"])
        print("Output tokens:", result.usage["output_tokens"])
        print("Total tokens:", result.usage["total_tokens"])
        print("===============================================")

        passed = (
            "BEDROCK_CONNECTION_OK"
            in result.text.upper()
        )

        if passed:
            print("")
            print("Bedrock connectivity test passed.")
            return 0

        print("")
        print(
            "Bedrock responded, but the expected test "
            "text was not returned."
        )
        return 1

    except Exception as error:
        print(
            f"\nBEDROCK CONNECTIVITY TEST FAILED: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())