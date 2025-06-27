import gradio as gr
import websockets
import asyncio
import os
import traceback
import struct
from typing import AsyncGenerator

# --- Configuration ---
WEBSOCKET_URI = os.getenv("WEBSOCKET_URI", "ws://localhost:8765")

# Audio properties must match the server's TTS output for correct WAV header creation
TTS_SAMPLE_RATE_HZ = int(os.getenv("TTS_SAMPLE_RATE_HZ", "44100"))
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH_BYTES = 2  # 16-bit audio
CHUNK_DURATION_S = 0.5
CHUNK_SIZE_BYTES = int(TTS_SAMPLE_RATE_HZ * TTS_CHANNELS * TTS_SAMPLE_WIDTH_BYTES * CHUNK_DURATION_S)

# --- Helper Function to create a WAV header ---
def _add_wav_header(data: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
    """
    Prepends a WAV header to raw PCM audio data.
    """
    datasize = len(data)
    # Total file size - 8 bytes for the RIFF header
    chunksize = 36 + datasize 
    # The format of the header is based on the WAV file specification.
    # See: http://soundfile.sapp.org/doc/WaveFormat/
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', chunksize, b'WAVE', b'fmt ', 16, 1,
        channels, sample_rate, sample_rate * channels * sample_width,
        channels * sample_width, sample_width * 8, b'data', datasize
    )
    return header + data

# --- WebSocket Producer (runs in the background) ---
async def _producer(filepath: str, event_queue: asyncio.Queue):
    """
    Connects to WebSocket, sends initial audio, receives streaming audio/status,
    and puts them into a single event queue. (This function remains unchanged).
    """
    try:
        async with websockets.connect(WEBSOCKET_URI, max_size=50 * 1024 * 1024, ping_interval=None) as websocket:
            await event_queue.put(("status", "Connection successful. Reading file..."))
            with open(filepath, "rb") as f:
                audio_bytes = f.read()
            
            await event_queue.put(("status", f"Sending {len(audio_bytes)} bytes to server..."))
            await websocket.send(audio_bytes)
            await event_queue.put(("status", "Audio sent. Receiving stream..."))

            while True:
                response = await websocket.recv()
                
                if response == "__END_OF_STREAM__":
                    await event_queue.put(("status", "End of stream signal received."))
                    await event_queue.put(("done", None))
                    break

                if isinstance(response, str):
                    await event_queue.put(("status", f"Server message: {response}"))
                    continue
                
                await event_queue.put(("audio", response))
                
    except Exception as e:
        error_msg = f"ERROR in producer: {e}\n{traceback.format_exc()}"
        await event_queue.put(("status", error_msg))
        await event_queue.put(("done", None))

# --- Gradio Event Handler (Consumer) ---
async def send_and_stream_audio_in_chunks(
    audio_filepath: str | None,
) -> AsyncGenerator[tuple[bytes | None, str], None]:
    """
    Orchestrates the WebSocket producer and consumes events from the queue
    to stream chunked audio (with WAV headers) to the Gradio UI.
    """
    if not audio_filepath:
        yield None, "Please provide an audio file or record your voice."
        return

    status_log = "Initializing...\n"
    audio_buffer = bytearray()
    
    event_queue = asyncio.Queue()
    producer_task = asyncio.create_task(_producer(audio_filepath, event_queue))

    try:
        while True:
            event_type, data = await event_queue.get()

            if event_type == "done":
                break
            
            if event_type == "status":
                status_log += f"{data}\n"
                yield None, status_log
                continue

            # Process audio data
            audio_buffer.extend(data)
            
            while len(audio_buffer) >= CHUNK_SIZE_BYTES:
                chunk_to_play = audio_buffer[:CHUNK_SIZE_BYTES]
                audio_buffer = audio_buffer[CHUNK_SIZE_BYTES:]
                
                # *** THE KEY FIX IS HERE ***
                # Add a WAV header to the raw PCM chunk before yielding
                wav_chunk_with_header = _add_wav_header(
                    bytes(chunk_to_play), 
                    TTS_SAMPLE_RATE_HZ, 
                    TTS_CHANNELS, 
                    TTS_SAMPLE_WIDTH_BYTES
                )
                
                status_log += f"Yielding WAV chunk of {len(wav_chunk_with_header)} bytes.\n"
                yield wav_chunk_with_header, status_log
        
        if audio_buffer:
            status_log += f"Yielding final remaining WAV chunk of {len(audio_buffer)} bytes.\n"
            final_wav_chunk = _add_wav_header(
                bytes(audio_buffer), 
                TTS_SAMPLE_RATE_HZ, 
                TTS_CHANNELS, 
                TTS_SAMPLE_WIDTH_BYTES
            )
            yield final_wav_chunk, status_log

        status_log += "--- Playback Complete ---\n"
        yield None, status_log

    finally:
        producer_task.cancel()
        print("Gradio handler finished and cleaned up.")


# --- Gradio UI ---
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        "# Real-time Audio AI Pipeline with Seamless Chunking\n\n"
        "Record or upload audio. The server streams back a response, and this app buffers it to play in smooth, 0.5-second chunks."
    )
    with gr.Row():
        with gr.Column(scale=1):
            input_audio = gr.Audio(sources=["microphone", "upload"], type="filepath", label="Your Voice Input")
            submit_btn = gr.Button("Submit", variant="primary")
        with gr.Column(scale=2):
            output_audio = gr.Audio(label="Synthesized Voice Output", streaming=True, autoplay=True)
            status_textbox = gr.Textbox(label="Processing Status Log", lines=12, interactive=False, autoscroll=True)

    submit_btn.click(
        fn=send_and_stream_audio_in_chunks,
        inputs=[input_audio],
        outputs=[output_audio, status_textbox],
        api_name="audio_chat_stream_chunked"
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0",server_port=8080)