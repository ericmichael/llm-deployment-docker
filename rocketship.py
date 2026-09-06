#!/usr/bin/env python3
"""
Rocketship - Azure App Service Deployment Helper

This script helps deploy applications to Azure App Service for Containers.
It handles:
- Docker image building and pushing to Azure Container Registry
- Azure App Service configuration with sidecar containers
- Remote access to the running container
- Backup download/upload via the Kudu (SCM) filesystem

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
    - A `.env.deploy` holding the production settings and the deploy
      credentials (registry password, subscription id). `.env` is for
      local development and is never read here.
"""

import argparse
import os
import re
import shutil
import subprocess

import requests
import yaml
from dotenv import dotenv_values, load_dotenv

# `.env.deploy` and nothing else. `.env` belongs to the local dev server, and
# this script reaching into it was the source of two separate problems: `.env`
# carried the production DATABASE_URL, so every `manage.py` run outside docker
# compose opened a connection to the Azure database; and everything in `.env`
# was uploaded as an App Service setting, so local-only values shipped
# straight to production, alongside deploy credentials the running app never
# reads.
#
# One file, and it says exactly what production gets.
load_dotenv(".env.deploy")


def azure_login():
    """Attempt to log into Azure CLI."""
    try:
        with open(os.devnull, "w") as devnull:
            subprocess.run(["az", "login"], stdout=devnull, stderr=devnull, check=True)
    except subprocess.CalledProcessError:
        print("Error logging into Azure.")
        return





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
                # Deliberately no per-setting retry. These values are secrets,
                # and passing them as arguments puts every one of them in the
                # process table where any user on the host can read them with
                # `ps`. The retry also used check=True inside a try that only
                # prints, so a failure part-way left half the settings pushed.
                print("      Error setting Azure app settings.")
                print(f"        {result.stderr.strip()}")
                return False
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
                print("      ✗ Error configuring sidecars")
                print(f"        {result.stderr}")
                return False

            print("      ✓ Sidecars configured")
            return True

        finally:
            os.unlink(spec_file)

    except subprocess.CalledProcessError as e:
        print("      ✗ Error configuring sidecars")
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
        "azure": {
            "subscription": "${AZURE_SUBSCRIPTION_ID}",
            "app_service": {
                "app_name": "${AZURE_APP_NAME}",
                "resource_group": "${AZURE_RESOURCE_GROUP}",
                "additional_env": {
                    # "true" mounts an Azure Files share at /home, which
                    # persists but adds network-filesystem latency. Leave it
                    # off unless something must outlive the container.
                    "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "false",
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

# Change to application directory
cd /usr/src/app

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
    motd = r'''
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
env | grep -E '^(ENVIRONMENT|SECRET_|DATABASE_|LITELLM_|AZURE_OPENAI_|OPENAI_|EASYAUTH_|WEBSITES_)' > /home/rocketship-env 2>/dev/null || true
chmod 644 /home/rocketship-env

# Start SSH service (required for Azure App Service SSH access)
service ssh start

# Run pre-start hook if it exists
if [ -x /usr/src/app/rocketship-hooks/pre-start ]; then
    /usr/src/app/rocketship-hooks/pre-start
fi

# Start the application
cd /usr/src/app
exec gunicorn gateway.asgi:application \\
    --bind 0.0.0.0:8000 \\
    --worker-class uvicorn.workers.UvicornWorker
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

# Example: read a secret from 1Password rather than keeping it in a file
# SECRET_KEY=$(op read "op://Vault/Item/field")

# Example: read from a file. Never commit one - .rocketship/ is gitignored
# for exactly this reason.
# SECRET_KEY=$(cat config/secret.key 2>/dev/null || echo "")
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
# python manage.py migrate

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
    print("  4. Set up your .env.deploy with the production + deploy variables")
    print("  5. Update your Dockerfile to copy from .rocketship/")
    print("\nSee the generated files for examples and documentation.")


#: Names that belong to a developer's machine or to the act of deploying, and
#: never to the application that ends up running. Everything in .env.deploy is
#: otherwise uploaded into the App Service configuration, which is how a
#: registry password ends up sitting in the config of the container it pulled -
#: readable by anyone who can read the configuration, and for no reason,
#: because the running app never asks for it.
#:
#: The Azure and LiteLLM credentials are NOT here and must not be: the embedded
#: proxy resolves AZURE_OPENAI_*, OPENAI_API_VERSION and LITELLM_MASTER_KEY from
#: the environment at boot, via os.environ/... in config/litellm-config.yaml.
NOT_FOR_DEPLOYMENT = (
    # The registry credentials this script logs in with.
    "ROCKETSHIP_",
    # Which subscription to deploy *to*. The app inside has no use for it.
    "AZURE_SUBSCRIPTION_ID",
)


def _deployable_env(values: dict) -> dict:
    """The `.env.deploy` entries that are app settings rather than tooling.

    The file holds both: what the running app reads, and what this script needs
    to talk to Azure and the registry. Only the first belongs in the App Service
    configuration - a subscription id and a registry password are credentials
    for the deploy, not for the thing deployed.
    """
    keep = {}
    dropped = []
    for name, value in values.items():
        if any(name.startswith(prefix) for prefix in NOT_FOR_DEPLOYMENT):
            dropped.append(name)
            continue
        keep[name] = value
    if dropped:
        print(f"      Not deploying (local or deploy-only): {', '.join(sorted(dropped))}")
    return keep


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
    print("Rocketship - Azure App Service deployment")
    print("=" * 60)

    print("\n[1/6] Checking Docker...")
    check_docker()
    print("      ✓ Docker found")

    print("[2/6] Checking Dockerfile...")
    check_dockerfile()
    print("      ✓ Dockerfile found")

    print("[3/6] Checking Azure CLI...")
    check_azure_cli()
    print("      ✓ Azure CLI found")

    print("[4/6] Loading config...")
    config = load_config()
    validate_config(config)
    print("      ✓ Config loaded and validated")

    registry = config.get("registry")
    image = config.get("image")
    azure = config.get("azure")

    print("[5/6] Loading .env.deploy variables...")
    # Only this file. `.env` is the dev server's, and it must not hold a
    # production DATABASE_URL either - every `manage.py` run outside docker
    # compose would open a connection to Azure.
    env_variables = _deployable_env(dotenv_values(".env.deploy"))
    print(f"      ✓ Loaded {len(env_variables)} variables")

    # Log into the registry and build/push image
    try:
        print(f'\n[6/6] Docker build & push to {registry["server"]}...')
        print('      Logging into registry...')
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
        print('      Pushing to registry...')
        for tag in tags:
            subprocess.run(["docker", "push", tag], check=True)
        print("      ✓ Image pushed")
    except subprocess.CalledProcessError:
        print("Error pushing Docker image to registry.")
        return

    # The tag Azure will pull — prefer SHA for cache-busting, fall back to latest
    deploy_tag = f"{image_base}:{git_sha}" if git_sha else f"{image_base}:latest"

    print('\nConfiguring Azure...')
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

        print('      Pushing settings to Azure App Service...')
        all_azure_secrets = {**env_variables, **azure["app_service"]["additional_env"]}

        # Merge sidecar env vars into app settings (they're shared with all containers)
        for sidecar_config in sidecars.values():
            sidecar_env = sidecar_config.get("env", {})
            all_azure_secrets.update(sidecar_env)

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

    print(f"Restarting {app_name}...")
    if restart_app_service(azure):
        print("✓ App Service restarted successfully")
    else:
        print("✗ Failed to restart App Service")
        exit(1)


def _vfs(remote_path):
    """Map an absolute container path onto Kudu's VFS, whose root is /home.

    "/api/vfs/home/backups/x" resolves to /home/home/backups/x and 404s; the
    correct URL is "/api/vfs/backups/x".
    """
    return remote_path[len("/home"):] if remote_path.startswith("/home/") else remote_path


def download(filename=None):
    """Download a file from the Kudu (SCM) filesystem.

    This is NOT the app container's filesystem - Kudu is a separate container
    with its own /home, so files here are not visible to the running app.
    """
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
            # `az ... --query '[a, b]' -o tsv` separates the two values with a
            # newline, not a tab. Splitting on "\t" always yielded one element,
            # so this branch failed for every invocation.
            creds = result.stdout.strip().splitlines()
            if len(creds) != 2:
                print("Error: Could not get publishing credentials")
                exit(1)

            username, password = creds

            # Download via Kudu VFS API
            kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs{_vfs(remote_path)}"

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
            creds = result.stdout.strip().splitlines()
            if len(creds) != 2:
                print("Error: Could not get publishing credentials")
                exit(1)
            username, password = creds
            list_backups_remote(app_name, username, password)
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
            exit(1)


def list_backups_remote(app_name, username, password):
    """List backup files in /home/backups on Azure."""
    kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs/backups/"

    try:
        response = requests.get(kudu_url, auth=(username, password))
        if response.status_code == 404:
            print("No backups directory found - nothing has written to /home/backups yet.")
            return

        response.raise_for_status()
        files = response.json()

        backups = [f for f in files if f["name"].startswith("backup-") and f["name"].endswith(".tar.gz")]

        if not backups:
            print("No backup files found in /home/backups/")
            print("Create one in the container, e.g. pg_dump \"$DATABASE_URL\" | gzip > /home/backups/backup-$(date +%F).sql.gz")
            return

        print("\nAvailable backups:")
        for backup in sorted(backups, key=lambda x: x["name"], reverse=True):
            size_kb = backup.get("size", 0) / 1024
            print(f"  {backup['name']}  ({size_kb:.2f} KB)")

        print("\nTo download, run:")
        print(f"  python rocketship.py download {backups[0]['name']}")

    except requests.RequestException as e:
        print(f"Error listing backups: {e}")


def upload(local_file, remote_filename=None):
    """Upload a file to /home/backups on the Kudu (SCM) filesystem.

    Not visible to the running app; see download().
    """
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
        creds = result.stdout.strip().splitlines()
        if len(creds) != 2:
            print("Error: Could not get publishing credentials")
            exit(1)
        username, password = creds

        # Ensure /home/backups directory exists
        kudu_dir_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs/backups/"
        requests.put(kudu_dir_url, auth=(username, password))

        # Upload via Kudu VFS API
        kudu_url = f"https://{app_name}.scm.azurewebsites.net/api/vfs{_vfs(remote_path)}"

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
        description="Rocketship - Azure App Service deployment helper"
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
