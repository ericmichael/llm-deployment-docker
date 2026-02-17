#!/usr/bin/env python3
"""
Rocketship - Azure App Service Deployment Helper

This script helps deploy applications to Azure App Service for Containers.
It handles:
- Docker image building and pushing to Azure Container Registry
- GitHub Actions secrets management
- Azure App Service configuration with sidecar containers
- Remote access to the running container
- Backup download/upload from persistent storage

Usage:
    python rocketship.py init              # Create starter config and .rocketship/
    python rocketship.py deploy            # Build, push, and deploy to Azure
    python rocketship.py ssh               # SSH into the running container
    python rocketship.py logs              # Stream live application logs
    python rocketship.py restart           # Restart the App Service
    python rocketship.py download          # List available backups
    python rocketship.py download <file>   # Download a backup file
    python rocketship.py upload <file>     # Upload a backup file

Prerequisites:
    - Docker installed
    - Azure CLI (`az`) installed and logged in
    - GitHub personal access token (GITHUB_TOKEN env var) - optional
    - pynacl package (`pip install pynacl`)
"""

from dotenv import load_dotenv, dotenv_values

load_dotenv()

import argparse
import subprocess
import yaml
import os
import re
import requests
import shutil
from base64 import b64encode

try:
    from nacl import encoding, public
except ImportError:
    print("Error: The 'nacl' library is required for this script.")
    print("You can install it with 'pip install pynacl'.")
    exit(1)


def azure_login():
    """Attempt to log into Azure CLI."""
    try:
        with open(os.devnull, "w") as devnull:
            subprocess.run(["az", "login"], stdout=devnull, stderr=devnull, check=True)
    except subprocess.CalledProcessError:
        print("Error logging into Azure.")
        return


def encrypt(public_key: str, secret_value: str) -> str:
    """Encrypt a Unicode string using the public key."""
    public_key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def create_secret(token, repo, name, value):
    """Create or update a GitHub Actions secret."""
    public_key = get_public_key(token, repo)
    encrypted_value = encrypt(public_key["key"], value)

    url = f"https://api.github.com/repos/{repo}/actions/secrets/{name}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {
        "encrypted_value": encrypted_value,
        "key_id": public_key["key_id"],
    }
    response = requests.put(url, headers=headers, json=data)
    response.raise_for_status()


def create_github_secrets(token, repo, secrets):
    """Create multiple GitHub Actions secrets."""
    for name, value in secrets.items():
        if value:  # Skip empty values
            create_secret(token, repo, name, str(value))


def update_app_settings(azure, additional_env):
    """Update Azure App Service application settings."""
    import json
    import tempfile

    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    if not additional_env:
        return True

    # Convert to list of {"name": key, "value": value, "slotSetting": false} format
    settings_list = [
        {"name": k, "value": str(v), "slotSetting": False}
        for k, v in additional_env.items() if v
    ]

    if not settings_list:
        return True

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(settings_list, f)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "az",
                    "webapp",
                    "config",
                    "appsettings",
                    "set",
                    "--name",
                    app_name,
                    "--resource-group",
                    resource_group,
                    "--settings",
                    f"@{temp_file}",
                    "--subscription",
                    subscription,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Fall back to individual settings if JSON fails
                print(f"      (JSON method failed: {result.stderr.strip()})")
                print("      Trying individual settings...")
                for k, v in additional_env.items():
                    if v:
                        subprocess.run(
                            [
                                "az", "webapp", "config", "appsettings", "set",
                                "--name", app_name,
                                "--resource-group", resource_group,
                                "--settings", f"{k}={v}",
                                "--subscription", subscription,
                            ],
                            capture_output=True,
                            check=True,
                        )
        finally:
            os.unlink(temp_file)
        return True
    except subprocess.CalledProcessError as e:
        print("      ✗ Error setting Azure app settings.")
        print(f"        {e}")
        return False


def configure_sidecars(azure, sidecars, registry):
    """Configure sidecar containers for Azure App Service using JSON spec file.

    Note: Sidecar env vars from azure-deploy.yml are pushed as app settings,
    which are automatically shared with all containers (main + sidecars).
    """
    if not sidecars:
        return True

    import json
    import tempfile

    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    # Build sidecar spec array
    sidecar_specs = []
    for name, config in sidecars.items():
        image = config.get("image")
        target_port = config.get("target_port", "5432")

        print(f"      Configuring sidecar: {name} ({image})")

        # Note: We don't set environmentVariables here because Azure App Service
        # automatically shares all app settings with sidecar containers.
        # The env vars from the sidecar config are pushed as app settings instead.
        spec = {
            "name": name,
            "properties": {
                "image": image,
                "targetPort": str(target_port),
                "isMain": False,
            }
        }

        sidecar_specs.append(spec)

    try:
        # Write spec to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(sidecar_specs, f)
            spec_file = f.name

        try:
            result = subprocess.run(
                [
                    "az", "webapp", "sitecontainers", "create",
                    "--name", app_name,
                    "--resource-group", resource_group,
                    "--subscription", subscription,
                    "--sitecontainers-spec-file", spec_file,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                print(f"      ✗ Error configuring sidecars")
                print(f"        {result.stderr}")
                return False

            print(f"      ✓ Sidecars configured")
            return True

        finally:
            os.unlink(spec_file)

    except subprocess.CalledProcessError as e:
        print(f"      ✗ Error configuring sidecars")
        print(f"        {e}")
        return False


def enable_sidecar_mode(azure):
    """Enable sidecar mode for the Azure App Service."""
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    try:
        # Check current linuxFxVersion
        result = subprocess.run(
            [
                "az", "webapp", "config", "show",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
                "--query", "linuxFxVersion",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
        )

        current_fx = result.stdout.strip()
        if "sitecontainers" in current_fx.lower():
            print("      ✓ Sidecar mode already enabled")
            return True

        # Enable sidecar mode by setting linuxFxVersion
        subprocess.run(
            [
                "az", "webapp", "config", "set",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
                "--linux-fx-version", "sitecontainers",
            ],
            capture_output=True,
            check=True,
        )
        print("      ✓ Sidecar mode enabled")
        return True

    except subprocess.CalledProcessError as e:
        print(f"      ✗ Error enabling sidecar mode: {e}")
        return False


def restart_app_service(azure):
    """Restart Azure App Service using stop/start for more reliable restart."""
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    try:
        # Stop the app first
        result = subprocess.run(
            [
                "az", "webapp", "stop",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"      ✗ Error stopping app: {result.stderr.strip()}")
            return False

        # Then start it again
        result = subprocess.run(
            [
                "az", "webapp", "start",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"      ✗ Error starting app: {result.stderr.strip()}")
            return False

        return True
    except subprocess.CalledProcessError as e:
        print("      ✗ Error restarting Azure Web App Service.")
        print(f"        {e}")
        return False


def update_container_image(azure, full_image_tag, registry):
    """Tell Azure App Service to pull a specific image tag.

    This is the key step that forces Azure to actually use the new image,
    rather than relying on :latest cache invalidation.
    """
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    try:
        result = subprocess.run(
            [
                "az", "webapp", "config", "container", "set",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
                "--container-image-name", full_image_tag,
                "--container-registry-url", f'https://{registry["server"]}',
                "--container-registry-user", registry["username"],
                "--container-registry-password", registry["password"],
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"      ✗ Error updating container image: {result.stderr.strip()}")
            return False
        return True
    except subprocess.CalledProcessError as e:
        print(f"      ✗ Error updating container image: {e}")
        return False


def get_git_sha():
    """Get short git SHA for image tagging."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_public_key(token, repo):
    """Get the public key for encrypting GitHub secrets."""
    url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def check_azure_cli():
    """Check if Azure CLI is installed."""
    if not shutil.which("az"):
        print("Error: Azure CLI is not installed or not in the system path.")
        print(
            "Please follow the installation instructions at: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
        )
        exit(1)


def check_docker():
    """Check if Docker is installed."""
    if not shutil.which("docker"):
        print("Error: Docker is not installed or not in the system path.")
        exit(1)


def check_dockerfile():
    """Check if Dockerfile exists."""
    if not os.path.isfile("Dockerfile"):
        print("Error: Dockerfile does not exist in the current directory.")
        exit(1)


def check_github():
    """Check if GitHub token is set. Returns True if set, False otherwise."""
    github_token = os.getenv("GITHUB_TOKEN")
    return bool(github_token and github_token != "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def init():
    """Create starter config/azure-deploy.yml and .rocketship/ directory."""

    # Create config/azure-deploy.yml
    config = {
        "image": "your-app/image",
        "registry": {
            "server": "${AZURE_REGISTRY_SERVER}",
            "username": "${AZURE_REGISTRY_USERNAME}",
            "password": "${ROCKETSHIP_REGISTRY_PASSWORD}",
        },
        "service": "your-app",
        "github": {"repo": "${GITHUB_REPO}"},
        "azure": {
            "subscription": "${AZURE_SUBSCRIPTION_ID}",
            "app_service": {
                "app_name": "${AZURE_APP_NAME}",
                "resource_group": "${AZURE_RESOURCE_GROUP}",
                "additional_env": {
                    "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "true",
                    "WEBSITES_PORT": "80",
                    "WEBSITES_CONTAINER_START_TIME_LIMIT": "300",
                },
            },
        },
    }

    os.makedirs("config", exist_ok=True)
    with open("config/azure-deploy.yml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print("Created config/azure-deploy.yml")

    # Create .rocketship/ directory structure
    os.makedirs(".rocketship/hooks", exist_ok=True)

    # Create .rocketship/profile.sh
    profile_sh = '''#!/bin/sh
# Rocketship SSH Profile
# This script is sourced on SSH login to set up the environment.
# Customize this file for your project's needs.

# Example: Ruby/Bundler configuration (uncomment for Rails apps)
# export BUNDLE_PATH=/usr/local/bundle
# export BUNDLE_WITHOUT=development
# export RAILS_ENV=production

# Change to application directory
cd /rails

# Source Azure environment variables saved by startup script
if [ -f /home/rocketship-env ]; then
    set -a
    . /home/rocketship-env
    set +a
fi
'''
    with open(".rocketship/profile.sh", "w") as f:
        f.write(profile_sh)
    print("Created .rocketship/profile.sh")

    # Create .rocketship/motd
    motd = '''
  ____            _        _       _     _
 |  _ \ ___   ___| | _____| |_ ___| |__ (_)_ __
 | |_) / _ \ / __| |/ / _ \ __/ __| '_ \| | '_ \
 |  _ < (_) | (__|   <  __/ |_\__ \ | | | | |_) |
 |_| \_\___/ \___|_|\_\___|\__|___/_| |_|_| .__/
                                          |_|

  Customize this message in .rocketship/motd

'''
    with open(".rocketship/motd", "w") as f:
        f.write(motd)
    print("Created .rocketship/motd")

    # Create .rocketship/startup.sh
    startup_sh = '''#!/bin/sh
# Rocketship Startup Script
# This script runs when the container starts.
# Customize this file for your project's needs.

set -e

# Export environment variables for SSH sessions
# Azure injects env vars only into the main process, so we save them for SSH access
# Customize the grep pattern to match your app's environment variable prefixes
env | grep -E '^(RAILS_|SECRET_|DATABASE_|REDIS_|WEBSITES_)' > /home/rocketship-env 2>/dev/null || true
chmod 644 /home/rocketship-env

# Start SSH service (required for Azure App Service SSH access)
service ssh start

# Run pre-start hook if it exists
if [ -x /rails/rocketship-hooks/pre-start ]; then
    /rails/rocketship-hooks/pre-start
fi

# Start the application
# Customize this command for your application
cd /rails
exec ./bin/rails server -b 0.0.0.0 -p 80
'''
    with open(".rocketship/startup.sh", "w") as f:
        f.write(startup_sh)
    print("Created .rocketship/startup.sh")

    # Create .rocketship/secrets
    secrets = '''# Rocketship Secrets
#
# Secrets defined here are available for reference in config/azure-deploy.yml
# using ${VAR_NAME} syntax. All secrets should be pulled from either a
# password manager, ENV, or a file. DO NOT ENTER RAW CREDENTIALS HERE!
# This file needs to be safe for git.
#
# This file is sourced as a shell script before deployment.

# Registry credentials (from environment)
ROCKETSHIP_REGISTRY_PASSWORD=$ROCKETSHIP_REGISTRY_PASSWORD

# Example: Read secrets from 1password
# RAILS_MASTER_KEY=$(op read "op://Vault/Item/field")

# Example: Read from file (never commit secrets to git!)
# RAILS_MASTER_KEY=$(cat config/master.key 2>/dev/null || echo "")
'''
    with open(".rocketship/secrets", "w") as f:
        f.write(secrets)
    print("Created .rocketship/secrets")

    # Create sample hooks
    pre_build_hook = '''#!/bin/sh
# Rocketship pre-build hook
# This script runs before the Docker image is built.
# Remove the .sample extension to activate.

# Example: Ensure git checkout is clean
# if [ -n "$(git status --porcelain)" ]; then
#     echo "Git checkout is not clean, aborting..." >&2
#     exit 1
# fi

exit 0
'''
    with open(".rocketship/hooks/pre-build.sample", "w") as f:
        f.write(pre_build_hook)

    post_deploy_hook = '''#!/bin/sh
# Rocketship post-deploy hook
# This script runs after successful deployment to Azure.
# Remove the .sample extension to activate.

# Example: Send notification
# echo "Deployed successfully"

exit 0
'''
    with open(".rocketship/hooks/post-deploy.sample", "w") as f:
        f.write(post_deploy_hook)

    pre_start_hook = '''#!/bin/sh
# Rocketship pre-start hook
# This script runs inside the container before the main application starts.
# Remove the .sample extension to activate.

# Example: Run database migrations
# ./bin/rails db:migrate

exit 0
'''
    with open(".rocketship/hooks/pre-start.sample", "w") as f:
        f.write(pre_start_hook)
    print("Created .rocketship/hooks/ with sample hooks")

    print("\n" + "=" * 60)
    print("Rocketship initialized!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Edit config/azure-deploy.yml with your Azure settings")
    print("  2. Edit .rocketship/profile.sh for your app's environment")
    print("  3. Edit .rocketship/startup.sh for your app's start command")
    print("  4. Set up your .env with the required variables")
    print("  5. Update your Dockerfile to copy from .rocketship/")
    print("\nSee the generated files for examples and documentation.")


def load_config():
    """Load and process the deployment configuration."""
    config_path = "config/azure-deploy.yml"
    if not os.path.isfile(config_path):
        print(f"Error: {config_path} does not exist.")
        print("Run 'python rocketship.py init' to create a starter config.")
        exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Replace placeholders with environment variable values
    config = replace_placeholders_in_dict(config)
    return config


def replace_placeholders(value):
    """Replace ${VAR_NAME} placeholders with environment variable values."""
    pattern = re.compile(r"\$\{(.+?)\}")
    return pattern.sub(lambda m: os.getenv(m.group(1), ""), value)


def replace_placeholders_in_dict(d):
    """Recursively replace placeholders in a dictionary."""
    for key, value in d.items():
        if isinstance(value, str):
            d[key] = replace_placeholders(value)
        elif isinstance(value, dict):
            d[key] = replace_placeholders_in_dict(value)
    return d


def validate_config(config):
    """Validate the deployment configuration."""
    assert (
        "service" in config and config["service"]
    ), "Missing or empty 'service' in config"
    assert "image" in config and config["image"], "Missing or empty 'image' in config"
    assert (
        "registry" in config and config["registry"]
    ), "Missing or empty 'registry' in config"
    assert (
        "server" in config["registry"] and config["registry"]["server"]
    ), "Missing or empty 'server' in registry config"
    assert (
        "username" in config["registry"] and config["registry"]["username"]
    ), "Missing or empty 'username' in registry config"
    assert (
        "password" in config["registry"] and config["registry"]["password"]
    ), "Missing or empty 'password' in registry config"


def setup(no_cache=False):
    """Run the full deployment setup."""
    print("=" * 60)
    print("Rocketship - Azure Deployment for Rails")
    print("=" * 60)

    print("\n[1/8] Checking Docker...")
    check_docker()
    print("      ✓ Docker found")

    print("[2/8] Checking Dockerfile...")
    check_dockerfile()
    print("      ✓ Dockerfile found")

    print("[3/7] Checking GitHub token (optional)...")
    has_github = check_github()
    if has_github:
        print("      ✓ GITHUB_TOKEN set")
    else:
        print("      ⚠ GITHUB_TOKEN not set, skipping GitHub secrets")

    print("[4/7] Checking Azure CLI...")
    check_azure_cli()
    print("      ✓ Azure CLI found")

    print("[5/7] Loading config...")
    config = load_config()
    validate_config(config)
    print("      ✓ Config loaded and validated")

    registry = config.get("registry")
    image = config.get("image")
    azure = config.get("azure")
    github = config.get("github")
    github_token = os.getenv("GITHUB_TOKEN")

    # Load environment variables from .env file
    print("[6/7] Loading .env variables...")
    env_variables = dotenv_values(".env")
    print(f"      ✓ Loaded {len(env_variables)} variables")

    # Log into the registry and build/push image
    try:
        print(f'\n[7/7] Docker build & push to {registry["server"]}...')
        print(f'      Logging into registry...')
        subprocess.run(
            [
                "docker",
                "login",
                registry["server"],
                "-u",
                registry["username"],
                "--password-stdin",
            ],
            input=registry["password"],
            encoding="utf-8",
            check=True,
            capture_output=True,
        )
        print("      ✓ Logged into registry")
    except subprocess.CalledProcessError as e:
        print(f"Error logging into Docker Container Registry: {e}")
        return

    # Generate image tags
    git_sha = get_git_sha()
    image_base = f'{registry["server"]}/{image}'
    tags = [f"{image_base}:latest"]
    if git_sha:
        tags.append(f"{image_base}:{git_sha}")
        print(f"      Image tag: {git_sha} (git SHA)")

    # Build the image
    try:
        print(f'      Building image: {image_base}')
        build_cmd = ["docker", "build"]
        for tag in tags:
            build_cmd.extend(["-t", tag])
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(".")
        subprocess.run(build_cmd, check=True)
        print("      ✓ Image built")
    except subprocess.CalledProcessError:
        print("Error building Docker image.")
        return

    # Push all tags to the registry
    try:
        print(f'      Pushing to registry...')
        for tag in tags:
            subprocess.run(["docker", "push", tag], check=True)
        print("      ✓ Image pushed")
    except subprocess.CalledProcessError:
        print("Error pushing Docker image to registry.")
        return

    # The tag Azure will pull — prefer SHA for cache-busting, fall back to latest
    deploy_tag = f"{image_base}:{git_sha}" if git_sha else f"{image_base}:latest"

    # Create and push secrets to Github (optional)
    print(f'\nConfiguring Azure (and GitHub if token provided)...')
    if has_github and github and github.get("repo"):
        print(f'      Pushing secrets to GitHub: {github["repo"]}')
        try:
            github_secrets = {
                "ROCKETSHIP_REGISTRY_SERVER": registry["server"],
                "ROCKETSHIP_REGISTRY_USERNAME": registry["username"],
                "ROCKETSHIP_REGISTRY_PASSWORD": registry["password"],
                "ROCKETSHIP_IMAGE": image,
            }
            all_github_secrets = {**env_variables, **github_secrets}
            all_github_secrets.pop("GITHUB_TOKEN", None)
            create_github_secrets(github_token, github["repo"], all_github_secrets)
            print("      ✓ GitHub secrets configured")
        except Exception as e:
            print(f"      ⚠ GitHub secrets failed: {e}")
            print("      Continuing with Azure deployment...")
    else:
        print("      ⚠ GitHub not configured, skipping secrets")

    if azure:
        # Check if sidecars are configured
        sidecars = azure.get("app_service", {}).get("sidecars", {})
        if sidecars:
            print("      Enabling sidecar mode...")
            if enable_sidecar_mode(azure):
                print("      Configuring sidecars...")
                if configure_sidecars(azure, sidecars, registry):
                    print("      ✓ Sidecars configured")
                else:
                    print("      ⚠ Sidecar configuration had issues")

        print(f'      Pushing settings to Azure App Service...')
        all_azure_secrets = {**env_variables, **azure["app_service"]["additional_env"]}

        # Merge sidecar env vars into app settings (they're shared with all containers)
        for sidecar_config in sidecars.values():
            sidecar_env = sidecar_config.get("env", {})
            all_azure_secrets.update(sidecar_env)

        # Remove GitHub token from Azure settings
        all_azure_secrets.pop("GITHUB_TOKEN", None)
        if update_app_settings(azure, all_azure_secrets):
            print("      ✓ Azure settings configured")

        print(f"      Updating container image to: {deploy_tag}")
        if update_container_image(azure, deploy_tag, registry):
            print("      ✓ Container image updated")
        else:
            print("      ✗ Failed to update container image")

        print("      Restarting Azure App Service...")
        if restart_app_service(azure):
            print("      ✓ App Service restarted")
    else:
        print("      ⚠ Azure config missing, skipping")

    print("\n" + "=" * 60)
    print("✓ Deployment setup complete!")
    print("=" * 60)
    if has_github and github and github.get("repo"):
        print("\nNext steps:")
        print("  1. Push to 'main' branch to trigger GitHub Actions deployment")
        print("  2. Monitor deployment at: https://github.com/{}/actions".format(github["repo"]))
    else:
        print("\nYour app has been deployed to Azure App Service.")
        print("To redeploy, run: python rocketship.py deploy")


def get_azure_config():
    """Load config and return azure settings."""
    config = load_config()
    azure = config.get("azure")
    if not azure:
        print("Error: No 'azure' configuration found in config/azure-deploy.yml")
        exit(1)
    return azure


def ssh():
    """SSH into the running Azure App Service container."""
    azure = get_azure_config()
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    print(f"Connecting to {app_name}...\n")

    subprocess.run([
        "az", "webapp", "ssh",
        "--name", app_name,
        "--resource-group", resource_group,
        "--subscription", subscription,
    ])


def logs():
    """Stream live logs from Azure App Service."""
    azure = get_azure_config()
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    print(f"Streaming logs from {app_name}... (Ctrl+C to stop)\n")
    try:
        subprocess.run([
            "az", "webapp", "log", "tail",
            "--name", app_name,
            "--resource-group", resource_group,
            "--subscription", subscription
        ])
    except KeyboardInterrupt:
        print("\nStopped log streaming.")


def restart():
    """Restart the Azure App Service."""
    azure = get_azure_config()
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    print(f"Restarting {app_name}...")
    if restart_app_service(azure):
        print("✓ App Service restarted successfully")
    else:
        print("✗ Failed to restart App Service")
        exit(1)


def download(filename=None):
    """Download a file from Azure App Service persistent storage (/home)."""
    azure = get_azure_config()
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    if filename:
        # Download specific file
        remote_path = f"/home/backups/{filename}" if not filename.startswith("/") else filename
        local_path = filename if "/" not in filename else os.path.basename(filename)

        print(f"Downloading {remote_path} from {app_name}...")

        # Use Kudu API to download file
        # First get publishing credentials
        try:
            result = subprocess.run(
                [
                    "az", "webapp", "deployment", "list-publishing-credentials",
                    "--name", app_name,
                    "--resource-group", resource_group,
                    "--subscription", subscription,
                    "--query", "[publishingUserName, publishingPassword]",
                    "-o", "tsv",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            creds = result.stdout.strip().split("\t")
            if len(creds) != 2:
                print("Error: Could not get publishing credentials")
                exit(1)

            username, password = creds

            # Download via Kudu VFS API
            kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs{remote_path}"

            response = requests.get(
                kudu_url,
                auth=(username, password),
                stream=True,
            )

            if response.status_code == 404:
                print(f"Error: File not found: {remote_path}")
                print("\nAvailable backups:")
                list_backups_remote(app_name, username, password)
                exit(1)

            response.raise_for_status()

            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = os.path.getsize(local_path)
            print(f"✓ Downloaded to: {local_path} ({file_size / 1024:.2f} KB)")

        except subprocess.CalledProcessError as e:
            print(f"Error getting credentials: {e}")
            exit(1)
        except requests.RequestException as e:
            print(f"Error downloading file: {e}")
            exit(1)
    else:
        # List available backups
        print(f"Listing backups on {app_name}...")
        try:
            result = subprocess.run(
                [
                    "az", "webapp", "deployment", "list-publishing-credentials",
                    "--name", app_name,
                    "--resource-group", resource_group,
                    "--subscription", subscription,
                    "--query", "[publishingUserName, publishingPassword]",
                    "-o", "tsv",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            creds = result.stdout.strip().split("\t")
            username, password = creds
            list_backups_remote(app_name, username, password)
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            exit(1)


def list_backups_remote(app_name, username, password):
    """List backup files in /home/backups on Azure."""
    kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs/home/backups/"

    try:
        response = requests.get(kudu_url, auth=(username, password))
        if response.status_code == 404:
            print("No backups directory found. Run 'bin/rails db:backup:export' first.")
            return

        response.raise_for_status()
        files = response.json()

        backups = [f for f in files if f["name"].startswith("backup-") and f["name"].endswith(".tar.gz")]

        if not backups:
            print("No backup files found in /home/backups/")
            print("Run 'bin/rails db:backup:export' in the container to create one.")
            return

        print("\nAvailable backups:")
        for backup in sorted(backups, key=lambda x: x["name"], reverse=True):
            size_kb = backup.get("size", 0) / 1024
            print(f"  {backup['name']}  ({size_kb:.2f} KB)")

        print(f"\nTo download, run:")
        print(f"  python rocketship.py download {backups[0]['name']}")

    except requests.RequestException as e:
        print(f"Error listing backups: {e}")


def upload(local_file, remote_filename=None):
    """Upload a file to Azure App Service persistent storage (/home/backups)."""
    azure = get_azure_config()
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]

    if not os.path.exists(local_file):
        print(f"Error: Local file not found: {local_file}")
        exit(1)

    remote_filename = remote_filename or os.path.basename(local_file)
    remote_path = f"/home/backups/{remote_filename}"

    print(f"Uploading {local_file} to {app_name}:{remote_path}...")

    try:
        # Get publishing credentials
        result = subprocess.run(
            [
                "az", "webapp", "deployment", "list-publishing-credentials",
                "--name", app_name,
                "--resource-group", resource_group,
                "--subscription", subscription,
                "--query", "[publishingUserName, publishingPassword]",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        creds = result.stdout.strip().split("\t")
        username, password = creds

        # Ensure /home/backups directory exists
        kudu_dir_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs/home/backups/"
        requests.put(kudu_dir_url, auth=(username, password))

        # Upload via Kudu VFS API
        kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs{remote_path}"

        with open(local_file, "rb") as f:
            response = requests.put(
                kudu_url,
                auth=(username, password),
                data=f,
                headers={"If-Match": "*"},
            )

        response.raise_for_status()

        file_size = os.path.getsize(local_file)
        print(f"✓ Uploaded: {remote_path} ({file_size / 1024:.2f} KB)")

    except subprocess.CalledProcessError as e:
        print(f"Error getting credentials: {e}")
        exit(1)
    except requests.RequestException as e:
        print(f"Error uploading file: {e}")
        exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Rocketship - Azure App Service Deployment Helper for Rails"
    )
    parser.add_argument(
        "command",
        choices=["init", "deploy", "ssh", "logs", "restart", "download", "upload"],
        help="Command to run"
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="File to download/upload (for download/upload commands)"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Build Docker image without using cache"
    )
    args = parser.parse_args()

    if args.command == "init":
        init()
    elif args.command == "deploy":
        setup(no_cache=args.no_cache)
    elif args.command == "ssh":
        ssh()
    elif args.command == "logs":
        logs()
    elif args.command == "restart":
        restart()
    elif args.command == "download":
        download(args.file)
    elif args.command == "upload":
        if not args.file:
            print("Error: Please specify a file to upload")
            print("Usage: python rocketship.py upload backup-20241218_120000.tar.gz")
            exit(1)
        upload(args.file)


if __name__ == "__main__":
    main()
