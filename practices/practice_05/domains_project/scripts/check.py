import subprocess
import sys
import os

def run_tests():
    print("Running tests...", file=sys.stderr)
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # Check if tests directory exists
    if not os.path.exists(os.path.join(project_dir, 'tests')):
        print("No tests directory found. Skipping tests.", file=sys.stderr)
        return
        
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests"], 
                            capture_output=True, text=True, cwd=project_dir)
    print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    
    if result.returncode == 0:
        print("OK: Все тесты пройдены!", file=sys.stderr)
    else:
        print("FAIL: Тесты упали.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
