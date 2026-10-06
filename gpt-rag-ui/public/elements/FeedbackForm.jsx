import React from "react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { Star, Send } from "lucide-react";

export default function FeedbackFormPopup(props) {
  const [open, setOpen] = React.useState(true);
  const [text, setText] = React.useState("");
  const [rating, setRating] = React.useState(0);
  const [hoverRating, setHoverRating] = React.useState(0);
  const [submitting, setSubmitting] = React.useState(false);

  const renderStars = () => {
  const stars = [];

  for (let i = 1; i <= 5; i++) {
    const isActive = i <= (hoverRating || rating);

    stars.push(
      <Star
        key={i}
        strokeWidth={2}
        stroke={isActive ? "#facc15" : "#d1d5db"}
        fill={isActive ? "#facc15" : "transparent"}
        className="h-7 w-7 cursor-pointer transition-all duration-200 transform hover:scale-110"
        onClick={() => setRating(i)}
        onMouseEnter={() => setHoverRating(i)}
        onMouseLeave={() => setHoverRating(0)}
      />
    );
  }
  return stars;
};


  const getRatingText = () => {
    const currentRating = hoverRating || rating;
    const labels = ["", "Poor", "Fair", "Good", "Very Good", "Excellent"];
    return labels[currentRating] || "Rate your experience";
  };

  const handleSubmit = async () => {
    if (rating === 0) return;
    setSubmitting(true);
    await callAction({
      name: "submit_feedback",
      payload: { text,
        rating,
  isPositive: props?.isPositive,
  questionId: props?.questionId,
  ask: props?.ask,
  auth_info: props?.authInfo, 
  conversationId: props?.conversationId 
      },
    });

    // Wait for a moment (optional)
    setTimeout(() => {
      setOpen(false);
      setSubmitting(false);
    }, 500);
  };

  const handleClose = async (val) => {
    if (!val) {
      // Dialog is being closed
      await callAction({
        name: "close_feedback_popup",
        payload: { questionId: props?.questionId },
      });
    }
    setOpen(val);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <div className="max-w-md mx-auto mt-4">
          <div className="bg-white rounded-2xl shadow-xl border border-gray-100 overflow-hidden">
            <div className="p-6 space-y-6">
              {/* Rating */}
              <div className="text-center">
                <div className="flex justify-center space-x-2 mb-3">
                  {renderStars()}
                </div>
                <p className="text-sm font-medium text-gray-600 h-5 transition-all duration-200">
                  {getRatingText()}
                </p>
              </div>
              {/* Comments */}
              <div className="space-y-3">
                <Label className="text-base font-medium text-gray-800 text-center w-full" />
                <Textarea
                  rows={4}
                  value={text}
                  placeholder="Share your thoughts, suggestions, or concerns..."
                  className="resize-none border-gray-200 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 rounded-xl transition-all duration-200"
                  onChange={(e) => setText(e.target.value)}
                />
              </div>
              {/* Submit */}
              
              <Button
                onClick={handleSubmit}
                disabled={rating === 0 || submitting}
                className="w-full bg-gradient-to-r from-blue-600 to-purple-600 hover:from-blue-700 hover:to-purple-700 text-white py-3 rounded-xl font-medium transition-all duration-200 transform hover:scale-[1.02] hover:shadow-lg disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none"
              >
                <Send className="h-5 w-5 mr-2" />
                {submitting ? "Submitting..." : "Submit Feedback"}
              </Button>
            </div>
            <div className="bg-gray-50 px-6 py-4 border-t border-gray-100">
              <p className="text-xs text-gray-500 text-center">
                Your feedback helps us create better experiences for everyone
              </p>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}