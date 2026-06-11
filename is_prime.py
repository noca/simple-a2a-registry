def is_prime(n: int) -> bool:
    """Check if a number is prime.

    Args:
        n: Integer to check.

    Returns:
        True if n is prime, False otherwise.
    """
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False

    # Check divisors up to sqrt(n); step by 2 to skip evens
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2

    return True