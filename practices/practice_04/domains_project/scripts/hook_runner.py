import json
import sys
import subprocess
import os

def main():
    # Read stdin to consume the input from the hook (required by contract)
    try:
        input_data = json.load(sys.stdin)
    except:
        pass
    
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check_script = os.path.join(project_dir, "scripts", "check.py")
    
    # Run the tests
    if os.path.exists(check_script):
        subprocess.run([sys.executable, check_script])
    
    # Output empty JSON for PostToolUse contract
    print(json.dumps({}))

if __name__ == "__main__":
    main()
