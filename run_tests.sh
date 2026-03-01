#!/bin/bash

# EagleAgent Test Runner
# Ensures Firestore emulator is running before executing tests

set -e  # Exit on error

echo "🧪 EagleAgent Test Runner"
echo "========================="

# Check if Firestore emulator is running
check_emulator() {
    ps aux | grep -q "[c]loud-firestore-emulator" && return 0 || return 1
}

# Start Firestore emulator
start_emulator() {
    echo "🚀 Starting Firestore emulator..."
    gcloud emulators firestore start --host-port=localhost:8686 > /tmp/firestore-emulator.log 2>&1 &
    
    # Wait for emulator to be ready
    echo "⏳ Waiting for emulator to start..."
    sleep 3
    
    # Verify it started
    if check_emulator; then
        echo "✅ Firestore emulator is running on localhost:8686"
    else
        echo "❌ Failed to start Firestore emulator"
        echo "📋 Check logs: tail /tmp/firestore-emulator.log"
        exit 1
    fi
}

# Main execution
if check_emulator; then
    echo "✅ Firestore emulator is already running"
else
    start_emulator
fi

echo ""
echo "🧪 Running tests..."
echo "==================="

# Run pytest with Firestore emulator environment variable
export FIRESTORE_EMULATOR_HOST=localhost:8686
uv run pytest tests/ -v "$@"

TEST_EXIT_CODE=$?

echo ""
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo "✅ All tests passed!"
else
    echo "❌ Some tests failed (exit code: $TEST_EXIT_CODE)"
fi

exit $TEST_EXIT_CODE
