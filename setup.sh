#!/bin/bash

# To Run, you should have GIT setup and configured the following:
# 1. git config --global user.email "you@example.com"
# 2. git config --global user.name "Your Name"
# Get user input for your name and email address
echo "Setup Initated: GIT"
# collect git username and email to new variables

echo "Your Name:"
read desired_name
echo "Your Email:"
read desired_email

# Get current Git name and email
current_name=$(git config --get user.name)
current_email=$(git config --get user.email)

# Function to update Git configuration
update_git_config() {
    echo "configuring..."
    git config user.name "$desired_name"
    git config user.email "$desired_email"
    echo "Updated"
}

# Check if Git name matches the desired name
if [[ "$current_name" != "$desired_name" ]]; then
    update_git_config
fi

# Check if Git email matches the desired email
if [[ "$current_email" != "$desired_email" ]]; then
    update_git_config
fi


pre-commit install

# Enable automatic setup of remote tracking branches
git config --global push.autoSetupRemote true