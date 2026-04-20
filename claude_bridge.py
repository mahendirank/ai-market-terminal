from executor import handle_output

user_input = input("Original task: ")
print("\nPaste Claude output below:\n")

claude_output = ""
while True:
    try:
        line = input()
        claude_output += line + "\n"
    except EOFError:
        break

handle_output(user_input, claude_output)
print("\n✅ Saved Claude output")
