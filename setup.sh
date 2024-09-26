# Define the line you want to add
add_docker="$USER ALL=(ALL) NOPASSWD: /usr/bin/docker"

# Check if the line already exists in the sudoers file
if ! sudo grep -qF "$add_docker" /etc/sudoers; then
    echo "$add_docker" | sudo tee -a /etc/sudoers
else
    echo "The $add_docker already exists in /etc/sudoers"
fi
