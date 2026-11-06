from limiter import Limiter

class Counter:
    def __init__(self):
        self.value = 0
        self.limiter = Limiter()

    def increment(self, amount=1):
        self.value = self.limiter.clamp(self.value + amount)
        return self.value

    def reset(self):
        self.value = 0
