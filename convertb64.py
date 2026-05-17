import base64

def txt_to_base64(input_path: str, output_path: str = None):
    with open(input_path, "r", encoding="utf-8") as f:
        text = f.read()

    encoded = base64.b64encode(text.encode("utf-8")).decode("utf-8")

    if output_path:
        with open(output_path, "w") as f:
            f.write(encoded)
        print(f"Saved to {output_path}")
    else:
        print(encoded)

# Usage
txt_to_base64("token.txt")              # prints to console