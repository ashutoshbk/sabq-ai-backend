Today's date is $today.

Use web search to find the $count most relevant and most recent trends or developments in security and policing technology, for the "Global Trends" section of a Dubai Police foresight dashboard.

Topic guidance:
- Focus on trends and developments, not breaking crime news or opinion pieces.
- Example themes (not a fixed list): predictive policing, AI in law enforcement, digital twins for security operations, autonomous surveillance, smart investigation and digital forensics, public-sector foresight, government performance and excellence.
- Prefer material published in roughly the last 12 months.
- Choose $count clearly different trends. Do not return the same trend twice.
- Prefer different sources for different trends where possible.

Strict rules:
- Use only pages that your web search returned. Never write a URL from memory.
- "source_url" must be the exact URL of a page you actually found and read.
- Do not invent statistics. Only state numbers that appear on the page, and keep summaries factual.
- Write the summary in your own words (2 to 3 sentences, max 400 characters). Do not copy text from the page.
- Describe only what the cited page actually says about security, policing or government. Do not stretch a general cybersecurity or business report into claims about police organizations.
- Attribute findings to the organization that published the page. If the page is hosted by one organization but written by another, say so briefly in the summary.
$extra

Return ONLY a JSON object, with no markdown fences and no other text, in exactly this shape:

{"trends": [
  {
    "title": "short trend title",
    "summary": "2 to 3 sentence summary in your own words",
    "category": "one of: Strategic Monitoring | Strategic Thinking | Policies | Performance Indicators | Global Competitiveness",
    "timeframe": "one of: Under 1 year | 1-2 years | 2-3 years | 3-5 years | 5+ years",
    "source_url": "exact URL of the page"
  }
]}

For "category", pick the area of a police planning department that the trend is most relevant to. For "timeframe", estimate when the trend is likely to matter at scale for a police organization.
