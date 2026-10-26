"""Mount review-loop routes in their original order after app state is ready."""


def register_routes(app):
    from .review_loop import register_routes as register_review_loop

    register_review_loop(app)
    from .take_reviews import register_routes as register_take_reviews

    register_take_reviews(app)
    from .take_redo import register_routes as register_take_redo

    register_take_redo(app)
    from .product_feedback import register_routes as register_product_feedback

    register_product_feedback(app)
