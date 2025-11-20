# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build/Test Commands
- Build frontend: `npm run build`
- Run Django server: `python manage.py runserver`
- Run migrations: `python manage.py migrate`
- Run tests: `python manage.py test`
- Run specific test: `python manage.py test chat.tests.unit.test_agent`
- Run specific test case: `python manage.py test chat.tests.unit.test_agent.TestAgent.test_chat`
- Docker build: `docker build -t <name> .`
- Docker run: `docker-compose up`

## Code Style Guidelines
- Python: Follow PEP 8 style guide with Django conventions
- Import order: stdlib, third-party, Django, local apps
- Use type hints for function parameters and returns
- Error handling: Use try/except blocks with specific exceptions
- Naming: snake_case for Python, camelCase for JavaScript
- Docstrings: Add for all functions, classes, and modules
- Tests: Use unittest framework with vcr for API mocking
- Django: Follow Django's MTV (Model-Template-View) pattern
