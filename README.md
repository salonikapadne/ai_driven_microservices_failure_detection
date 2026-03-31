# Josh-Hack submission: Vector Syndicates

---

## Project Overview
This is a modular application designed to orchestrate interactions between a React-based frontend, backend AI-based engines, and dummy services, all brought together using Dockerized services. This project demonstrates how a production grade AI system could self heal microservice architecture systems.

Currently we have implemented the following cases where the AI self detects and applies the fixes:
- **Container shutdown**
- **DB Instance shutdown**
- **DB Failure**
- **Buggy Code**

### Functionality
1. **Frontend (React)**:
   - Provides the user-facing application where users interact with the services.
   - Interfaces with the AI Engine for dynamic responses and data.

2. **AI Engine**:
   - Processes requests from the frontend.
   - Executes machine learning models and logic to provide intelligent data for the frontend.

3. **Dummy Services**:
   - Contains mock implementations of various APIs designed to simulate real-world services for the purpose of demonstrating the project’s workflow.

---

## Setup Instructions

1. **Frontend Dependencies**:
    - Navigate to the `frontend` directory.
    - Install the dependencies:
      ```bash
      npm install
      ```

2. **AI Engine Dependencies**:
    - Navigate to the `ai_engine` directory.
    - Activate your Python virtual environment.
    - Install the required packages:
      ```bash
      pip install -r requirements.txt
      ```

3. **Initialize the Entire Project**:
    - From the root directory, run:
      ```bash
      docker compose up --build
      ```

---

## File Structure

```plaintext
josh-hack/
├── frontend/       # Contains the frontend implementation
├── ai_engine/      # Backend AI-related services and virtual environment setup
├── ai-services/    # Legacy AI services; not actively used
├── dummy_services/ # Mock APIs for demo purposes
├── docker-compose.yml # Docker configuration file
└── README.md       # Project Documentation
```
