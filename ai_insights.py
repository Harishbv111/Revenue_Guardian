from ai_recovery import client, MODEL_NAME

def generate_fallback_insights(metrics):
    breakdown = metrics.get("failure_breakdown", {})
    biggest_reason = metrics.get("top_failure_reason", "Unknown")
    biggest_amount = 0
    biggest_cases = 0
    for reason, data in breakdown.items():
        amount = float(data.get("amount", 0))
        if amount > biggest_amount:
            biggest_amount = amount
            biggest_cases = data.get("cases", 0)
            biggest_reason = reason
    return f"""Revenue Guardian data-based recovery analysis.\n\n### Overall Assessment\n\n• Revenue at risk: ₹{metrics.get('revenue_at_risk',0):,.0f}\n• Expected recovery: ₹{metrics.get('expected_recovery',0):,.0f}\n• Recovered revenue: ₹{metrics.get('recovered_revenue',0):,.0f}\n• Recovery rate: {metrics.get('recovery_rate',0):.2f}%\n\n### Biggest Revenue Problem\n\n• Top failure reason: {biggest_reason}\n• Cases affected: {biggest_cases}\n• Amount affected: ₹{biggest_amount:,.0f}\n\n### Recommended Actions\n\n1. Prioritize cases with the highest expected recovery value.\n2. Address the failure category with the largest revenue exposure.\n3. Execute suitable recovery actions and record the actual outcome.\n\n### Estimated Business Impact\n\n• Current expected recovery opportunity: ₹{metrics.get('expected_recovery',0):,.0f}\n"""

def generate_merchant_insights(metrics):
    if client is None:
        return generate_fallback_insights(metrics)
    prompt = f"""You are Revenue Guardian, an AI revenue recovery analyst. Use ONLY these merchant metrics:\nRevenue at risk: ₹{metrics['revenue_at_risk']}\nExpected recovery: ₹{metrics['expected_recovery']}\nRecovered revenue: ₹{metrics['recovered_revenue']}\nTotal cases: {metrics['total_cases']}\nActive failed cases: {metrics['failed_cases']}\nRecovered cases: {metrics['recovered_cases']}\nRecovery rate: {metrics['recovery_rate']}%\nFailure breakdown: {metrics['failure_breakdown']}\nTop failure reason: {metrics['top_failure_reason']}\n\nProvide: 1. Overall assessment 2. Biggest revenue problem 3. Why it is happening based only on supplied categories 4. Three recommended actions 5. Estimated business impact. Never invent numbers and clearly distinguish at-risk from recovered revenue."""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        if response and response.text:
            return response.text
    except Exception as error:
        print(f"Gemini unavailable: {error}")
    return generate_fallback_insights(metrics)
