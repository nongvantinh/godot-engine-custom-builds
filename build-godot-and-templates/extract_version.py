import sys
import os

GODOT_SOURCE_DIR = os.path.join(os.getcwd(), 'git')
sys.path.insert(0, GODOT_SOURCE_DIR)

try:
    import version
except ImportError as e:
    print(f"Error importing version: {e}")
    sys.exit(1)

def get_version():
    if hasattr(version, "patch") and version.patch != 0:
        git_version = f"{version.major}.{version.minor}.{version.patch}"
    else:
        git_version = f"{version.major}.{version.minor}"
    
    return git_version

def get_version_status():
    return f"{version.status}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_version.py --get-version | --get-version-status")
        sys.exit(1)

    command = sys.argv[1]

    if command == '--get-version':
        print(get_version())
    elif command == '--get-version-status':
        print(get_version_status())
    else:
        print("Invalid command. Use --get-version or --get-version-status.")
        sys.exit(1)
