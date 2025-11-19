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
    for name, value in secrets.items():
        create_secret(token, repo, name, value)


def update_app_settings(azure, additional_env):
    app_name = azure["app_service"]["app_name"]
    resource_group = azure["app_service"]["resource_group"]
    subscription = azure["subscription"]  # Add the subscription to your config

    for key, value in additional_env.items():
        try:
            with open(os.devnull, "w") as devnull:
                subprocess.run(
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
                        f"{key}={value}",
                        "--subscription",
                        subscription,
                    ],
                    stdout=devnull,
                    stderr=devnull,
                    check=True,
                )
        except subprocess.CalledProcessError as e:
            print(f"Error setting app setting {key}.")
            print(e)
            return


def get_public_key(token, repo):
    url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def check_azure_cli():
    if not shutil.which("az"):
        print("Error: Azure CLI is not installed or not in the system path.")
        print(
            "Please follow the installation instructions at: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
        )
        exit(1)


def check_docker():
    if not shutil.which("docker"):
        print("Error: Docker is not installed or not in the system path.")
        exit(1)


def check_dockerfile():
    if not os.path.isfile("Dockerfile"):
        print("Error: Dockerfile does not exist in the current directory.")
        exit(1)


def check_github():
    # Load the Github token from an environment variable
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is not set.")
        exit(1)


def init():
    config = {
        "service": "myapp",
        "image": "image-name",
        "registry": {
            "server": "registry.server.com",
            "username": "registry-user-name",
            "password": ["ROCKETSHIP_REGISTRY_PASSWORD"],
        },
        "github": {"repo": "org-or-user/repo-name"},
    }

    os.makedirs("config", exist_ok=True)
    with open("config/deploy.yml", "w") as f:
        yaml.dump(config, f)


def load_config():
    with open("config/deploy.yml") as f:
        config = yaml.safe_load(f)

    # Replace placeholders with environment variable values
    config = replace_placeholders_in_dict(config)
    return config


def replace_placeholders(value):
    # This regular expression matches placeholders like ${VAR_NAME}
    pattern = re.compile(r"\$\{(.+?)\}")
    return pattern.sub(lambda m: os.getenv(m.group(1)), value)


def replace_placeholders_in_dict(d):
    for key, value in d.items():
        if isinstance(value, str):
            d[key] = replace_placeholders(value)
        elif isinstance(value, dict):
            d[key] = replace_placeholders_in_dict(value)
    return d


def validate_config(config):
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


def setup():
    print("Checking Docker...")
    check_docker()
    print("Checking Dockerfile...")
    check_dockerfile()
    print("Checking Github CLI...")
    check_github()
    print("Checking Azure CLI...")
    check_azure_cli()

    print("Loading config...")
    config = load_config()
    print("Validating config...")
    validate_config(config)

    registry = config["registry"] if "registry" in config else None
    image = config["image"] if "image" in config else None
    service = config["service"] if "service" in config else None
    azure = config["azure"] if "azure" in config else None
    github = config["github"] if "github" in config else None
    github_token = os.getenv("GITHUB_TOKEN")

    # Load environment variables from .env file
    print("Loading .env")
    env_variables = dotenv_values(".env")

    # if azure:
    #     print("Attempting Azure Login")
    #     azure_login()

    # Log into the registry
    try:
        print(f'Logging into Docker Container Registry: {registry["server"]} ...')
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
        )
    except subprocess.CalledProcessError:
        print("Error logging into Docker Container Registry.")
        return

    # Build the image
    try:
        print(f'Building Docker image: {registry["server"]}/{image}:latest ...')
        subprocess.run(
            ["docker", "build", "-t", f'{registry["server"]}/{image}:latest', "."],
            check=True,
        )
    except subprocess.CalledProcessError:
        print("Error building Docker image.")
        return

    # Push the image to the registry
    try:
        print(f'Pushing Docker image: {registry["server"]}/{image}:latest ...')
        subprocess.run(
            ["docker", "push", f'{registry["server"]}/{image}:latest'], check=True
        )
    except subprocess.CalledProcessError:
        print("Error pushing Docker image to registry.")
        return

    # Create and push secrets to Github
    if github:
        print(f'Pushing secrets to Github repository: {github["repo"]} ...')

        github_secrets = {
            "ROCKETSHIP_REGISTRY_SERVER": registry["server"],
            "ROCKETSHIP_REGISTRY_USERNAME": registry["username"],
            "ROCKETSHIP_REGISTRY_PASSWORD": registry["password"],
            "ROCKETSHIP_IMAGE": image,
        }
        all_github_secrets = {**env_variables, **github_secrets}
        all_github_secrets.pop("GITHUB_TOKEN", None)
        create_github_secrets(github_token, github["repo"], all_github_secrets)
    else:
        print("Missing configuration for Github...")
        print("Pushing secrets to Github Repository:...skipped")

    if azure:
        print(f"Pushing secrets to Azure Web App Service ...")
        all_azure_secrets = {**env_variables, **azure["app_service"]["additional_env"]}
        update_app_settings(azure, all_azure_secrets)
    else:
        print("Missing configuration for Azure...")
        print("Pushing secrets to Azure Web App Service...skipped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "setup"])
    args = parser.parse_args()

    if args.command == "init":
        init()
    elif args.command == "setup":
        setup()


if __name__ == "__main__":
    main()
