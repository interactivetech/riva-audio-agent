import asyncio
import os
import wave
import tempfile
import traceback
import websockets
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# Riva client imports
import riva.client
from riva.client.proto.riva_audio_pb2 import AudioEncoding

# OpenAI Client
try:
    from openai import OpenAI
except ImportError:
    logging.error("OpenAI library not found. Please install it: pip install openai")
    OpenAI = None

def get_bool_env(var_name: str, default: bool = False) -> bool:
    return os.getenv(var_name, str(default)).lower() in ('true', '1', 't', 'yes', 'y')

LLM_API_BASE = os.getenv("LLM_API_BASE")
LLM_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_PROMPT_TEMPLATE = os.getenv("LLM_PROMPT_TEMPLATE", 'Answer the question: "{transcript}"\n\nAnswer concisely.')
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "meta-llama/Llama-3.2-1B-Instruct")

ASR_SERVER_ADDRESS = os.getenv("ASR_SERVER_ADDRESS")
ASR_USE_SSL = get_bool_env("ASR_USE_SSL", False)
ASR_LANGUAGE_CODE = os.getenv("ASR_LANGUAGE_CODE", "en-US")

TTS_SERVER_ADDRESS = os.getenv("TTS_SERVER_ADDRESS")
TTS_USE_SSL = get_bool_env("TTS_USE_SSL", False)
TTS_VOICE = os.getenv("TTS_VOICE", "Magpie-Multilingual.EN-US.Sofia")
TTS_LANGUAGE_CODE = os.getenv("TTS_LANGUAGE_CODE", "en-US")
TTS_SAMPLE_RATE_HZ = int(os.getenv("TTS_SAMPLE_RATE_HZ", "44100"))

SENTENCE_TERMINATORS = ['.', '?', '!']

def inspect_wav_properties(filepath: str) -> tuple[int, int, str]:
    if not os.path.exists(filepath):
        return 0, 0, f"Error: File not found: {filepath}"
    try:
        with wave.open(filepath, 'rb') as wf:
            rate, channels = wf.getframerate(), wf.getnchannels()
            status = f"Audio Properties - Rate: {rate}Hz, Channels: {channels}, Width: {wf.getsampwidth()*8}-bit"
            return rate, channels, status
    except wave.Error as e:
        return 0, 0, f"Error inspecting WAV file: {e}"

async def stream_tts_for_sentence(websocket, tts_service, text_to_synthesize, stream_start_time, metrics):
    if not text_to_synthesize.strip():
        return
    logging.info(f"[TTS Task] Dispatching: \"{text_to_synthesize}\"")
    try:
        responses = tts_service.synthesize_online(
            text=text_to_synthesize,
            voice_name=TTS_VOICE,
            language_code=TTS_LANGUAGE_CODE,
            sample_rate_hz=TTS_SAMPLE_RATE_HZ,
            encoding=AudioEncoding.LINEAR_PCM
        )
        first_chunk = True
        for resp in responses:
            if resp.audio:
                current_time = time.time()
                if first_chunk and metrics['llm_tts_metrics']['first_tts_chunk_time'] is None:
                    metrics['llm_tts_metrics']['first_tts_chunk_time'] = current_time - stream_start_time
                    first_chunk = False
                metrics['llm_tts_metrics']['tts_chunk_latencies'].append(current_time - stream_start_time)
                await websocket.send(resp.audio)
    except Exception as e:
        logging.error(f"[TTS Task] Error during synthesis for '{text_to_synthesize}': {e}")

async def main_pipeline(websocket, audio_bytes: bytes):
    metrics = {
        'asr_processing_time': 0,
        'llm_tts_metrics': {
            'first_llm_token_time': None,
            'llm_token_latencies': [],
            'first_tts_chunk_time': None,
            'tts_chunk_latencies': [],
        }
    }
    tts_tasks, input_audio_file = [], None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            input_audio_file = tmp.name
            tmp.write(audio_bytes)

        sample_rate, n_channels, inspect_status = inspect_wav_properties(input_audio_file)
        logging.info(inspect_status)
        if sample_rate == 0:
            await websocket.send(inspect_status)
            return

        asr_start_time = time.time()
        asr_service = riva.client.ASRService(riva.client.Auth(uri=ASR_SERVER_ADDRESS, use_ssl=ASR_USE_SSL))
        with wave.open(input_audio_file, 'rb') as wav_f:
            audio_data_for_asr = wav_f.readframes(wav_f.getnframes())

        logging.info(f"Attempting offline ASR with audio at {sample_rate}Hz...")
        asr_config = riva.client.RecognitionConfig(
            encoding=riva.client.AudioEncoding.LINEAR_PCM, sample_rate_hertz=sample_rate,
            language_code=ASR_LANGUAGE_CODE, max_alternatives=1,
            enable_automatic_punctuation=True, audio_channel_count=n_channels
        )
        response = asr_service.offline_recognize(audio_data_for_asr, asr_config)
        metrics['asr_processing_time'] = time.time() - asr_start_time
        final_transcript = " ".join([res.alternatives[0].transcript for res in response.results if res.alternatives])

        if not final_transcript:
            await websocket.send("Error: ASR produced no transcript.")
            return
        logging.info(f"ASR Transcript: \"{final_transcript}\"")

        llm_tts_start_time = time.time()
        llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_API_BASE)
        tts_service = riva.client.SpeechSynthesisService(riva.client.Auth(uri=TTS_SERVER_ADDRESS, use_ssl=TTS_USE_SSL))

        prompt = LLM_PROMPT_TEMPLATE.format(transcript=final_transcript)
        logging.info("--- Starting LLM -> TTS Streaming (Buffering full sentences) ---")
        llm_stream = llm_client.chat.completions.create(model=LLM_MODEL_NAME, messages=[{"role": "user", "content": prompt}], temperature=0.2, stream=True)

        sentence_buffer = ""
        for chunk in llm_stream:
            if metrics['llm_tts_metrics']['first_llm_token_time'] is None:
                metrics['llm_tts_metrics']['first_llm_token_time'] = time.time() - llm_tts_start_time
            metrics['llm_tts_metrics']['llm_token_latencies'].append(time.time() - llm_tts_start_time)

            token = chunk.choices[0].delta.content
            if token:
                sentence_buffer += token
                while True:
                    split_pos = -1
                    for delim in SENTENCE_TERMINATORS:
                        pos = sentence_buffer.find(delim)
                        if pos != -1 and (split_pos == -1 or pos < split_pos):
                            split_pos = pos
                    if split_pos != -1:
                        dispatch_text = sentence_buffer[:split_pos + 1]
                        sentence_buffer = sentence_buffer[split_pos + 1:]
                        task = asyncio.create_task(stream_tts_for_sentence(websocket, tts_service, dispatch_text, llm_tts_start_time, metrics))
                        tts_tasks.append(task)
                    else:
                        break
        llm_end_time = time.time()

        if sentence_buffer.strip():
            task = asyncio.create_task(stream_tts_for_sentence(websocket, tts_service, sentence_buffer, llm_tts_start_time, metrics))
            tts_tasks.append(task)

        await asyncio.gather(*tts_tasks)
        tts_end_time = time.time()

        m = metrics['llm_tts_metrics']
        logging.info("--- Latency Report ---")
        logging.info(f"ASR Time: {metrics['asr_processing_time']:.4f}s")
        logging.info(f"LLM -> TTS:")
        logging.info(f"  First LLM Token:   {m['first_llm_token_time']:.4f}s" if m['first_llm_token_time'] else "N/A")
        logging.info(f"  First TTS Chunk:   {m['first_tts_chunk_time']:.4f}s" if m['first_tts_chunk_time'] else "N/A")
        logging.info(f"  Avg Token Latency: {sum(m['llm_token_latencies'])/len(m['llm_token_latencies']):.4f}s" if m['llm_token_latencies'] else "N/A")
        logging.info(f"  Avg TTS Latency:   {sum(m['tts_chunk_latencies'])/len(m['tts_chunk_latencies']):.4f}s" if m['tts_chunk_latencies'] else "N/A")
        logging.info("--- End of Report ---")

        await websocket.send("__END_OF_STREAM__")
        logging.info("Pipeline finished.\n")

    except Exception as e:
        err = f"Pipeline error: {e}\n{traceback.format_exc()}"
        logging.error(err)
        try:
            await websocket.send(f"Server Error: {e}")
        except websockets.ConnectionClosed:
            pass
    finally:
        for task in tts_tasks:
            task.cancel()
        if input_audio_file and os.path.exists(input_audio_file):
            try:
                os.remove(input_audio_file)
            except OSError as e:
                logging.warning(f"Failed to remove temp file: {e}")

async def handler(websocket):
    logging.info(f"Client connected: {websocket.remote_address}")
    try:
        audio_bytes = await websocket.recv()
        logging.info(f"Received {len(audio_bytes)} bytes")
        await main_pipeline(websocket, audio_bytes)
    except websockets.ConnectionClosed:
        logging.info(f"Client disconnected: {websocket.remote_address}")
    except Exception as e:
        logging.error(f"Error with client {websocket.remote_address}: {e}")

async def main():
    host, port = "0.0.0.0", 8765
    logging.info(f"🟢 Starting WebSocket server on ws://{host}:{port}")
    async with websockets.serve(handler, host, port, max_size=50 * 1024 * 1024):
        await asyncio.Future()

if __name__ == "__main__":
    if OpenAI is None:
        exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shutdown.")
