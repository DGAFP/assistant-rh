export interface CircuitBreakerOptions {
	cooldownMs?: number;
	now?: () => number;
}

export interface CircuitBreakerSnapshot {
	isOpen: boolean;
	openUntil: number;
	remainingMs: number;
}

const DEFAULT_COOLDOWN_MS = 60_000;

/**
 * Lightweight cooldown circuit breaker.
 *
 * - `recordFailure` opens the breaker for `cooldownMs`
 * - `recordSuccess` closes it immediately
 * - `shouldSkip` returns true while open
 */
export class CircuitBreaker {
	private readonly cooldownMs: number;

	private readonly now: () => number;

	private openUntil = 0;

	constructor(options: CircuitBreakerOptions = {}) {
		this.cooldownMs = options.cooldownMs ?? DEFAULT_COOLDOWN_MS;
		this.now = options.now ?? (() => Date.now());
	}

	shouldSkip(): boolean {
		return this.now() < this.openUntil;
	}

	recordFailure(): void {
		this.openUntil = this.now() + this.cooldownMs;
	}

	recordSuccess(): void {
		this.openUntil = 0;
	}

	snapshot(): CircuitBreakerSnapshot {
		const now = this.now();
		const remainingMs = Math.max(0, this.openUntil - now);

		return {
			isOpen: remainingMs > 0,
			openUntil: this.openUntil,
			remainingMs,
		};
	}
}
