---
name: "play-youtube-video"
skill: "play_youtube_video"
args:
  - name: "search_query"
    type: "string"
    required: true
    default: null
    description: "The search term to look up on YouTube, e.g., 'latest trailer' or a movie name."
description: >
  Searches YouTube for a given query and plays the first video result. Use
  when the user wants to watch or play a video on YouTube based on a search
  term. Not for downloading videos or extracting transcripts.
---

## When to Use

When the user asks to play or watch a YouTube video by searching for a topic, title, or keyword.

## When NOT to Use

When the user provides a direct YouTube URL, wants to download a video, or needs a transcript.

## Step Pattern

1. Navigate to https://www.youtube.com/results?search_query={search_query} using browser_navigate.
2. Wait 3 seconds using browser_wait for the search results to load.
3. Identify the first video link in the search results (e.g., via the accessibility tree or page text) and navigate to its URL using browser_navigate.
4. Wait 3 seconds using browser_wait for the video page to load and start playing.

## Args Detail

- `search_query` (string, required): The search term to look up on YouTube, e.g., 'latest trailer' or a movie name.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If the search results do not load, retry after waiting longer. If no video link is found, try a different search query or check if YouTube is blocking automated access. If the video page fails to load, navigate back to search results and select another video.
