<!-- File: helm/README.md -->

# Helm Chart for Audio Pipeline

This Helm chart (`audio-pipeline-chart`) deploys both the WebSocket server and the Gradio UI on any Kubernetes cluster (designed for HPE PCAI).

## Prerequisites

- Helm 3.x  
- A Kubernetes cluster with Istio (for the VirtualService)  
- The models deployed in PCAI (see root README)

## Customize

- Edit `values.yaml` to set image tags, resource requests/limits, and environment variables.  
- Ensure `ezua.virtualService.endpoint` and `istioGateway` match your PCAI ingress.

## Package for PCAI

```bash
cd helm
helm package .
```
This produces audio-pipeline-chart-0.1.0.tgz.

## Import into PCAI

In the PCAI Console, go to Tools and Frameworks → Import Framework.

Step 1: Enter Framework Name, Description, and upload a Logo.

Step 2: Upload the audio-pipeline-chart-0.1.0.tgz and choose a Namespace (e.g. riva).

Step 3: Review the rendered values.yaml and update the openaiApiKey field with a key generated in the HPE Machine Learning Inference Software Tool.

Click Deploy.

You can now launch the audio conversational agent from PCAI.