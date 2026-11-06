from limiter import Limiter

class Counter:
    def __init__(self):
        self.value = 0
        self.limiter = Limiter()

    def increment(self, amount=1):
        # BUG: bypasses the project limiter
        self.value = self.value + amount
        return self.value

    def reset(self):
        self.value = 0
