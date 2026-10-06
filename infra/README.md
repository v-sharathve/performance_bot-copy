# Azure AI Landing Zone

## Overview

The Azure AI Landing Zone is an enterprise-scale, production-ready reference architecture designed to deploy secure and resilient AI applications and agents on Azure. This repository contains the Bicep implementation, the Terraform implementations are available in separate repositories.   

![Architecture Diagram](media/Architecture%20Diagram.png)

## How to Deploy

Choose your preferred deployment method based on project requirements and environment constraints.

### Prerequisites

**Required Permissions:**

- Azure subscription with **Contributor** and **User Access Admin** roles
- Agreement to Responsible AI terms for Azure AI Services

**Required Tools:**

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- [Azure Developer CLI](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd)
- [Git](https://git-scm.com/downloads)

> Azure CLI is included as a prerequisite for future pre/post provisioning hooks that may depend on it.

### Basic Deployment

Quick setup for demos without network isolation.

**Initialize the project**

```
azd init -t azure/bicep-ptn-aiml-landing-zone
```

**Sign in to Azure**

```
az login
azd auth login
```

> Add `--tenant` for `az` or `--tenant-id` for `azd` if you want a specific tenant.

**Provision Infrastructure**

```
azd provision
```
> **Optional:** You can change parameter values in `main.parameters.json` or set them using `azd env set` before running `azd provision`. The latter applies only to parameters that support environment variable substitution.

### Zero Trust Deployment

For deployments that **require network isolation**.

**Before Provisioning**

Enable network isolation in your environment:

```
azd env set NETWORK_ISOLATION true
```

> **Optional:** Update other parameters in `main.parameters.json` or via `azd env set` before provisioning.

Make sure you're signed in with your Azure user account:

```
az login
azd auth login
```

> Add `--tenant` for `az` or `--tenant-id` for `azd` if you want a specific tenant.

**Provision Infrastructure**

```
azd provision
```

**Using the Jumpbox VM**

1. **Reset the VM password** in the Azure Portal (required on first access if not set in deployment parameters):

   - Go to your VM resource → **Support + troubleshooting** → **Reset password** → Set new credentials
   - Default username is `testvmuser`

2. **Connect via Azure Bastion**

### Permissions

The following role assignments are provisioned by the template based on the **default configuration** in `main.parameters.json`. This includes the default set of container apps, their associated roles, and the services they interact with. If you customize the parameters before provisioning — such as adding or removing container apps or changing role mappings — the actual assignments will vary accordingly.

#### Microsoft Foundry and AI Search Assignments

| Resource | Role | Assignee | Description |
| --- | --- | --- | --- |
| Microsoft Foundry Account | Cognitive Services User | Search Service | Allow Search Service to access vectorizers |
| GenAI App Search Service | Search Index Data Reader | Microsoft Foundry Project | Read index data |
| GenAI App Search Service | Search Service Contributor | Microsoft Foundry Project | Create AI Search connection |
| GenAI App Storage Account | Storage Blob Data Reader | Microsoft Foundry Project | Read blob data |
| GenAI App Storage Account | Storage Blob Data Reader | Search Service | Read blob data for indexing |

#### Container App Role Assignments

| Resource | Role | Assignee | Description |
| --- | --- | --- | --- |
| GenAI App Configuration Store | App Configuration Data Reader | ContainerApp: orchestrator | Read configuration data |
| GenAI App Configuration Store | App Configuration Data Reader | ContainerApp: frontend | Read configuration data |
| GenAI App Configuration Store | App Configuration Data Reader | ContainerApp: dataingest | Read configuration data |
| GenAI App Configuration Store | App Configuration Data Reader | ContainerApp: mcp | Read configuration data |
| GenAI App Container Registry | AcrPull | ContainerApp: orchestrator | Pull container images |
| GenAI App Container Registry | AcrPull | ContainerApp: frontend | Pull container images |
| GenAI App Container Registry | AcrPull | ContainerApp: dataingest | Pull container images |
| GenAI App Container Registry | AcrPull | ContainerApp: mcp | Pull container images |
| GenAI App Key Vault | Key Vault Secrets User | ContainerApp: orchestrator | Read secrets |
| GenAI App Key Vault | Key Vault Secrets User | ContainerApp: frontend | Read secrets |
| GenAI App Key Vault | Key Vault Secrets User | ContainerApp: dataingest | Read secrets |
| GenAI App Key Vault | Key Vault Secrets User | ContainerApp: mcp | Read secrets |
| GenAI App Search Service | Search Index Data Reader | ContainerApp: orchestrator | Read index data |
| GenAI App Search Service | Search Index Data Contributor | ContainerApp: dataingest | Read/write index data |
| GenAI App Search Service | Search Index Data Contributor | ContainerApp: mcp | Read/write index data |
| GenAI App Storage Account | Storage Blob Data Reader | ContainerApp: orchestrator | Read blob data |
| GenAI App Storage Account | Storage Blob Data Reader | ContainerApp: frontend | Read blob data |
| GenAI App Storage Account | Storage Blob Delegator | ContainerApp: frontend | Delegate blob access |
| GenAI App Storage Account | Storage Blob Data Contributor | ContainerApp: dataingest | Read/write blob data |
| GenAI App Storage Account | Storage Blob Data Contributor | ContainerApp: mcp | Read/write blob data |
| GenAI App Cosmos DB | Cosmos DB Built-in Data Contributor | ContainerApp: orchestrator | Read/write Cosmos DB data |
| GenAI App Cosmos DB | Cosmos DB Built-in Data Contributor | ContainerApp: dataingest | Read/write Cosmos DB data |
| GenAI App Cosmos DB | Cosmos DB Built-in Data Contributor | ContainerApp: mcp | Read/write Cosmos DB data |
| Microsoft Foundry Account | Cognitive Services User | ContainerApp: orchestrator | Access Cognitive Services |
| Microsoft Foundry Account | Cognitive Services User | ContainerApp: dataingest | Access Cognitive Services |
| Microsoft Foundry Account | Cognitive Services User | ContainerApp: mcp | Access Cognitive Services |
| Microsoft Foundry Account | Cognitive Services OpenAI User | ContainerApp: orchestrator | Use OpenAI APIs |
| Microsoft Foundry Account | Cognitive Services OpenAI User | ContainerApp: dataingest | Use OpenAI APIs |
| Microsoft Foundry Account | Cognitive Services OpenAI User | ContainerApp: mcp | Use OpenAI APIs |

#### Executor Role Assignments

| Resource | Role | Assignee | Description |
| --- | --- | --- | --- |
| GenAI App Configuration Store | App Configuration Data Owner | Executor | Full control over configuration settings |
| GenAI App Container Registry | AcrPush | Executor | Push container images |
| GenAI App Container Registry | AcrPull | Executor | Pull container images |
| GenAI App Key Vault | Key Vault Contributor | Executor | Manage Key Vault settings |
| GenAI App Key Vault | Key Vault Secrets Officer | Executor | Create Key Vault secrets |
| GenAI App Search Service | Search Service Contributor | Executor | Create/update search service elements |
| GenAI App Search Service | Search Index Data Contributor | Executor | Read/write search index data |
| GenAI App Search Service | Search Index Data Reader | Executor | Read index data |
| GenAI App Storage Account | Storage Blob Data Contributor | Executor | Read/write blob data |
| GenAI App Cosmos DB | Cosmos DB Built-in Data Contributor | Executor | Read/write Cosmos DB data |
| Microsoft Foundry Account | Cognitive Services OpenAI User | Executor | Use OpenAI APIs |
| Microsoft Foundry Account | Cognitive Services User | Executor | Access Cognitive Services |

#### Jumpbox VM Role Assignments

| Resource | Role | Assignee | Description |
| --- | --- | --- | --- |
| GenAI App Container Apps | Container Apps Contributor | Jumpbox VM | Full control over Container Apps |
| Azure Managed Identity | Managed Identity Operator | Jumpbox VM | Assign and manage user-assigned identities |
| GenAI App Container Registry | Container Registry Repository Writer | Jumpbox VM | Write to ACR repositories |
| GenAI App Container Registry | Container Registry Tasks Contributor | Jumpbox VM | Manage ACR tasks |
| GenAI App Container Registry | Container Registry Data Access Configuration Administrator | Jumpbox VM | Manage ACR data access configuration |
| GenAI App Container Registry | AcrPush | Jumpbox VM | Push container images |
| GenAI App Configuration Store | App Configuration Data Owner | Jumpbox VM | Full control over configuration settings |
| GenAI App Key Vault | Key Vault Contributor | Jumpbox VM | Manage Key Vault settings |
| GenAI App Key Vault | Key Vault Secrets Officer | Jumpbox VM | Create Key Vault secrets |
| GenAI App Search Service | Search Service Contributor | Jumpbox VM | Create/update search service elements |
| GenAI App Search Service | Search Index Data Contributor | Jumpbox VM | Read/write search index data |
| GenAI App Storage Account | Storage Blob Data Contributor | Jumpbox VM | Read/write blob data |
| GenAI App Cosmos DB | Cosmos DB Built-in Data Contributor | Jumpbox VM | Read/write Cosmos DB data |
| Microsoft Foundry Account | Cognitive Services Contributor | Jumpbox VM | Manage Cognitive Services resources |
| Microsoft Foundry Account | Cognitive Services OpenAI User | Jumpbox VM | Use OpenAI APIs |
