#!/usr/bin/env python3
"""
CivicPulse Canary Deployment Controller
Automated Canary Traffic Splitting, Telemetry Health Evaluation, and Promotion/Rollback

Features:
- Supports ALL microservices: Gateway, Grievance, Notification, Auth, Frontend
- Deploys canary side-by-side with stable deployment via Helm
- Collects live Actuator health, HTTP error rate (5xx), latency, pod readiness, and restarts
- Evaluates health across configurable intervals against defined SLA thresholds
- Autonomous Decision Engine:
  - PROMOTE: updates stable deployment to the new image and tears down canary
  - ROLLBACK: instantly removes canary, restores last known-good revision, records trigger reason,
    decision timestamp, rollback duration, and final health
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

SERVICE_PORTS = {
    "frontend": 80,
    "auth-service": 8081,
    "grievance-service": 8082,
    "notification-service": 8083,
    "gateway-service": 8089,
}

SERVICE_KEYS = {
    "frontend": "frontend",
    "auth-service": "auth",
    "grievance-service": "grievance",
    "notification-service": "notification",
    "gateway-service": "gateway",
}

def run_cmd(cmd, check=True):
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed (exit {res.returncode}): {cmd}\nStderr: {res.stderr}\nStdout: {res.stdout}")
    return res

def get_stable_image(namespace, service):
    cmd = f"kubectl get deployment {service} -n {namespace} -o jsonpath='{{.spec.template.spec.containers[0].image}}'"
    return run_cmd(cmd).stdout.strip()

def get_canary_pod_info(namespace, service):
    cmd = f"kubectl get pods -l app={service},track=canary -n {namespace} -o json"
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
        items = data.get("items", [])
        if not items:
            return None
        pod = items[0]
        name = pod["metadata"]["name"]
        phase = pod["status"].get("phase", "Unknown")
        restarts = 0
        ready = False
        container_statuses = pod["status"].get("containerStatuses", [])
        if container_statuses:
            cs = container_statuses[0]
            restarts = cs.get("restartCount", 0)
            ready = cs.get("ready", False)
        return {
            "name": name,
            "phase": phase,
            "ready": ready,
            "restarts": restarts
        }
    except Exception as e:
        return None

def test_endpoint_via_cluster(namespace, url, target_service):
    """Executes an HTTP probe from inside the cluster"""
    # Probe from gateway-service if targeting frontend; probe from frontend otherwise
    probe_pod = "deployment/gateway-service" if target_service == "frontend" else "deployment/frontend"
    cmd = f"kubectl exec -n {namespace} {probe_pod} -- wget -S -O - --timeout=5 {url} 2>&1"
    start = time.time()
    res = run_cmd(cmd, check=False)
    latency_ms = (time.time() - start) * 1000.0
    
    output = res.stdout + res.stderr
    status_code = 0
    for line in output.splitlines():
        m = re.search(r"HTTP/\S+\s+(\d{3})", line)
        if m:
            status_code = int(m.group(1))
            break
                
    return status_code, latency_ms, output

def main():
    parser = argparse.ArgumentParser(description="CivicPulse Canary Deployment Controller")
    parser.add_argument("--service", default="gateway-service", help="Target service name")
    parser.add_argument("--canary-image", required=True, help="Image tag to test in canary")
    parser.add_argument("--namespace", default="civicpulse", help="Kubernetes namespace")
    parser.add_argument("--chart-dir", default="./civicpulse-infrastructure/civicpulse-app", help="Path to Helm chart")
    parser.add_argument("--values-file", default="./civicpulse-infrastructure/civicpulse-app/values-civicpulse.yaml", help="Path to values.yaml")
    parser.add_argument("--intervals", type=int, default=5, help="Number of evaluation intervals")
    parser.add_argument("--interval-sec", type=int, default=3, help="Seconds between intervals")
    parser.add_argument("--max-error-rate", type=float, default=0.0, help="Max allowed error rate (0.0 = 0%)")
    parser.add_argument("--max-latency-ms", type=float, default=1500.0, help="Max allowed latency in ms")
    args = parser.parse_args()

    port = SERVICE_PORTS.get(args.service, 8080)
    service_key = SERVICE_KEYS.get(args.service, args.service)
    health_path = "/health" if args.service == "frontend" else "/actuator/health"

    print("=" * 60)
    print("CIVICPULSE CANARY DEPLOYMENT CONTROLLER")
    print("=" * 60)
    print(f"Target Service   : {args.service} (port {port}, key {service_key})")
    print(f"Canary Image     : {args.canary_image}")
    print(f"Health Endpoint  : {health_path}")
    print(f"Namespace        : {args.namespace}")
    print(f"Eval Intervals   : {args.intervals} x {args.interval_sec}s")
    print(f"Error Threshold  : {args.max_error_rate * 100}%")
    print(f"Latency Threshold: {args.max_latency_ms} ms")
    
    # 1. Baseline
    stable_image = get_stable_image(args.namespace, args.service)
    print(f"Current Stable   : {stable_image}")
    
    # 2. Deploy Canary
    print(f"\n[Step 1/4] Deploying Canary for {args.service} alongside Stable release via Helm...")
    helm_cmd = (
        f"helm upgrade civicpulse-app {args.chart_dir} -n {args.namespace} -f {args.values_file} "
        f"--set canary.enabled=true --set canary.service={args.service} --set canary.image={args.canary_image} "
        f"--set canary.port={port} --set canary.healthPath={health_path}"
    )
    run_cmd(helm_cmd)
    print("✓ Helm release updated with canary deployment")

    # Wait for Canary pod to be scheduled and reach Running
    print("Waiting for canary pod to schedule and initialize...")
    pod_info = None
    for attempt in range(25):
        time.sleep(2)
        pod_info = get_canary_pod_info(args.namespace, args.service)
        if pod_info:
            print(f"  Canary Pod: {pod_info['name']} (Phase: {pod_info['phase']}, Ready: {pod_info['ready']})")
            if pod_info["ready"]:
                break
            if pod_info["phase"] in ["Failed", "Error"]:
                break
    
    # 3. Telemetry & Health Evaluation
    print(f"\n[Step 2/4] Evaluating Telemetry & Health Signals ({args.intervals} intervals)...")
    eval_results = []
    failed_reasons = []

    for i in range(1, args.intervals + 1):
        time.sleep(args.interval_sec)
        pod_info = get_canary_pod_info(args.namespace, args.service)
        if not pod_info:
            eval_results.append({"interval": i, "status": "PodNotFound", "error": True})
            failed_reasons.append(f"Interval {i}: Canary pod not found")
            continue

        pod_ready = pod_info["ready"]
        pod_restarts = pod_info["restarts"]
        
        # Test endpoint
        probe_url = f"http://{args.service}-canary:{port}{health_path}"
        status_code, latency_ms, body = test_endpoint_via_cluster(
            args.namespace, probe_url, args.service
        )
        
        actuator_status = "DOWN"
        if '"status":"UP"' in body or '"UP"' in body or status_code == 200:
            actuator_status = "UP"

        is_error = False
        error_msg = ""
        if status_code >= 500 or status_code == 0:
            is_error = True
            error_msg = f"HTTP status {status_code}"
        elif latency_ms > args.max_latency_ms:
            is_error = True
            error_msg = f"Latency {latency_ms:.1f}ms exceeds threshold"
        elif not pod_ready:
            is_error = True
            error_msg = f"Pod not ready"
        elif pod_restarts > 0:
            is_error = True
            error_msg = f"Pod restarted ({pod_restarts} restarts)"
        elif actuator_status != "UP":
            is_error = True
            error_msg = "Health status not UP"

        result_entry = {
            "interval": i,
            "pod": pod_info["name"],
            "ready": pod_ready,
            "restarts": pod_restarts,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "actuator_status": actuator_status,
            "error": is_error,
            "error_msg": error_msg
        }
        eval_results.append(result_entry)

        status_str = f"HTTP {status_code}" if status_code else "CONN_ERR"
        print(f"  [Interval {i}/{args.intervals}] Pod: {pod_info['name']} | Ready: {pod_ready} | "
              f"Restarts: {pod_restarts} | {status_str} | Latency: {latency_ms:.1f}ms | Health: {actuator_status}")

        if is_error:
            failed_reasons.append(f"Interval {i}: {error_msg}")

    # 4. Decision & Execution
    total_evals = len(eval_results)
    error_count = sum(1 for r in eval_results if r.get("error", False))
    error_rate = error_count / total_evals if total_evals > 0 else 1.0

    print("\n[Step 3/4] Evaluating Decision Rules...")
    print(f"Total Intervals: {total_evals}")
    print(f"Error Count    : {error_count}")
    print(f"Error Rate     : {error_rate * 100:.1f}% (Threshold: {args.max_error_rate * 100}%)")

    should_rollback = error_rate > args.max_error_rate or len(failed_reasons) >= 2

    report = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "service": args.service,
        "target_canary_image": args.canary_image,
        "stable_image_baseline": stable_image,
        "telemetry_intervals": eval_results,
        "summary": {
            "total_intervals": total_evals,
            "error_count": error_count,
            "error_rate": error_rate,
            "reasons": failed_reasons
        }
    }

    if should_rollback:
        decision_timestamp = datetime.utcnow().isoformat() + "Z"
        trigger_reason = "; ".join(failed_reasons) if failed_reasons else "Exceeded error rate threshold"
        print("\n" + "!" * 60)
        print("DECISION: AUTOMATIC ROLLBACK TRIGGERED")
        print(f"Trigger Reason: {trigger_reason}")
        print("!" * 60)
        
        rollback_start = time.time()
        print("\n[Step 4/4] Executing Automatic Rollback...")
        # Rollback Helm canary configuration
        rollback_cmd = (
            f"helm upgrade civicpulse-app {args.chart_dir} -n {args.namespace} -f {args.values_file} "
            f"--set canary.enabled=false"
        )
        run_cmd(rollback_cmd)
        print("✓ Disabled canary in Helm release")

        # Wait for canary pod termination
        print("Waiting for canary pod to terminate...")
        run_cmd(f"kubectl delete pod -l app={args.service},track=canary -n {args.namespace} --ignore-not-found=true")
        
        # Verify stable rollout is healthy
        run_cmd(f"kubectl rollout status deployment/{args.service} -n {args.namespace} --timeout=60s")
        rollback_duration = time.time() - rollback_start
        print(f"✓ Rollback complete in {rollback_duration:.2f} seconds")

        report["decision"] = "ROLLBACK"
        report["decision_timestamp"] = decision_timestamp
        report["trigger_reason"] = trigger_reason
        report["rollback_duration_seconds"] = round(rollback_duration, 2)
        report["final_health"] = "HEALTHY"
        report["active_image"] = stable_image

        print("\n" + "=" * 60)
        print("ROLLBACK AUDIT LOG")
        print(json.dumps(report, indent=2))
        print("=" * 60)

        os.makedirs("./civicpulse-infrastructure/artifacts", exist_ok=True)
        with open(f"./civicpulse-infrastructure/artifacts/{args.service}_canary_rollback_evidence.json", "w") as f:
            json.dump(report, f, indent=2)

        sys.exit(1)

    else:
        print("\n" + "*" * 60)
        print("DECISION: PROMOTE CANARY TO PRODUCTION")
        print("Reason: All telemetry health checks and SLA thresholds passed successfully")
        print("*" * 60)

        promo_start = time.time()
        print("\n[Step 4/4] Executing Automatic Promotion...")
        try:
            with open(args.values_file, "r") as vf:
                vcontent = vf.read()
            pattern = rf"({service_key}:\s*\n(?:[^\n]*\n)*?\s*image:\s*)[^\n]+"
            new_vcontent = re.sub(pattern, rf"\g<1>{args.canary_image}", vcontent)
            with open(args.values_file, "w") as vf:
                vf.write(new_vcontent)
        except Exception:
            pass

        promote_cmd = (
            f"helm upgrade civicpulse-app {args.chart_dir} -n {args.namespace} -f {args.values_file} "
            f"--set services.{service_key}.image={args.canary_image} --set canary.enabled=false --force-conflicts"
        )
        run_cmd(promote_cmd)
        print(f"✓ Updated Helm stable service image to {args.canary_image}")

        print("Waiting for stable deployment rollout...")
        run_cmd(f"kubectl rollout status deployment/{args.service} -n {args.namespace} --timeout=120s")
        promo_duration = time.time() - promo_start
        print(f"✓ Promotion rollout complete in {promo_duration:.2f} seconds")

        report["decision"] = "PROMOTE"
        report["promotion_timestamp"] = datetime.utcnow().isoformat() + "Z"
        report["promotion_duration_seconds"] = round(promo_duration, 2)
        report["final_health"] = "HEALTHY"
        report["active_image"] = args.canary_image

        print("\n" + "=" * 60)
        print("PROMOTION AUDIT LOG")
        print(json.dumps(report, indent=2))
        print("=" * 60)

        os.makedirs("./civicpulse-infrastructure/artifacts", exist_ok=True)
        with open(f"./civicpulse-infrastructure/artifacts/{args.service}_canary_promotion_evidence.json", "w") as f:
            json.dump(report, f, indent=2)

        sys.exit(0)

if __name__ == "__main__":
    main()
