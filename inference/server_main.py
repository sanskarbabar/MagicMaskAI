"""Entry point for the frozen service executable (aicutout-service.exe)."""
import multiprocessing

from inference.server import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
