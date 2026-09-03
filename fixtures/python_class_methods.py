class Container:
    def outer_method(self):
        return 1

    def nested_inner(self):
        def inner():
            return 2
        return inner()

class Empty:
    pass
