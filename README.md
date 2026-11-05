# Agentic Parts Controller

## Overview

Agentic Parts Controller is an agentic AI-based prototype designed to support automated parts management and decision-making through structured workflows.

The project focuses on interpreting part requirements, validating technical specifications, checking supporting documentation, and managing parts-related decisions through a controlled agentic workflow.

## Problem Statement

Parts management workflows often involve checking technical specifications, reviewing approval documents, validating requirements, and handling missing or conflicting information.

Manual processing can be time-consuming and error-prone.

This project explores an agent-driven approach to make these workflows more structured, consistent, and efficient.

## Key Features

- **Document Validation:** Identifies and processes relevant approved request documents.
- **Specification Extraction:** Extracts technical specifications from document text.
- **Requirement Validation:** Checks whether parts satisfy specified requirements.
- **Clarification Handling:** Processes additional information when requirements are unclear.
- **Agentic Workflow:** Uses structured actions to manage multi-step operations.
- **Simulation Environment:** Supports local testing using simulated scenarios.
- **Error Handling:** Handles invalid operations and unsuccessful workflow conditions.

## Technology Stack

- Python 3
- JSON
- Agentic workflow logic
- Git and GitHub

## Project Structure

```text
AgenticPartsController/
│
├── solution.py
├── initial/
│   ├── agentic_simulator.py
│   ├── local_runner.py
│   ├── smoke.json
│   └── training.json
│
└── README.md
```

## Getting Started

### Prerequisites

- Python 3.9 or later
- Git
- Ubuntu / Linux or Windows with WSL

### Installation

Clone the repository:

```bash
git clone https://github.com/ramyahegde-109/AgenticPartsController.git
```

Navigate to the project:

```bash
cd AgenticPartsController
```

### Running the Project

Compile-check the Python solution:

```bash
python3 -m py_compile solution.py
```

Run the local smoke test in mock mode:

```bash
python3 initial/local_runner.py solution.py initial/smoke.json --mock
```

## Testing

The project includes local simulation scenarios for testing the workflow.

**Note:** Mock-mode execution is simulation-only. It does not establish successful communication with a real gateway or qualify as an official protocol test.

## Project Status

Currently under development.
