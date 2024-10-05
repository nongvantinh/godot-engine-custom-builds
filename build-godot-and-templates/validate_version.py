import sys
import os

GODOT_SOURCE_DIR = os.path.join(os.getcwd(), 'git')
sys.path.insert(0, GODOT_SOURCE_DIR)

try:
    import version
except ImportError as e:
    print(f"Error importing version: {e}")
    sys.exit(1)

def validate_version(godot_version):
    if hasattr(version, "patch") and version.patch != 0:
        git_version = f"{version.major}.{version.minor}.{version.patch}"
    else:
        git_version = f"{version.major}.{version.minor}"
    
    return git_version == godot_version

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python validate_version.py <godot_version>")
        sys.exit(1)

    godot_version = sys.argv[1]
    if validate_version(godot_version):
        print("Version is valid.")
    else:
        print(f"Version does not match: expected {godot_version}.")
